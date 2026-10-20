"""POST /api/chat (docs/api.md → Chat) against a real Postgres: contract shape, postbacks, danger, rate limit.

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
        {"session_id": sid, "message": "น้ำจะถึงไหม", "postback": "memory:delete_all"},
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
    for data in ("hack:me", "triage:confirm", "triage:cancel"):          # no rescue requests to confirm any more
        r = _chat(client, {"session_id": str(uuid.uuid4()), "postback": data})
        assert r.status_code == 400 and r.json()["error"]["code"] == "bad_request", data


def test_question_about_a_station_returns_reply_sources_and_quick_replies(seeded):
    client, _ = seeded
    r = _chat(client, {"session_id": str(uuid.uuid4()), "message": "บ้านแม่อยู่บางบาล อยุธยา น้ำจะถึงไหม"})
    assert r.status_code == 200
    body = r.json()
    assert "ingested_at" in body["meta"]
    d = body["data"]
    assert set(d) == {"reply", "sources", "quick_replies", "emergency", "place"}
    # The point the answer is about, so the web can open the flood map there; a name gives a district centre.
    assert "บางบาล" in d["place"]["label"] and d["place"]["approx"] is True
    assert set(d["place"]) == {"label", "lat", "lon", "approx"}
    # The web draws HotlineNotice under answers with a station source: no text copy, no emoji (LINE keeps both).
    # The station reading and the shelter are cards, so the text has the verdict instead of the station lines.
    assert "1784" not in d["reply"] and not chat._EMOJI.search(d["reply"])
    assert "สถานีทดสอบ น้ำ" in d["reply"] and "T.1" not in d["reply"] and "ศูนย์ทดสอบ" not in d["reply"]
    assert "สิ่งที่ควรทำตอนนี้\n• " in d["reply"]
    station = next(s for s in d["sources"] if s["type"] == "station")
    assert station["station_id"] == STATION and station["code"] == "T.1" and station["name_th"] == "สถานีทดสอบ"
    assert 0 < station["distance_km"] < 60 and station["observed_at"]
    # The reading behind the web StationCard: same numbers as the DB, bank = level - diff.
    assert station["river"] == "แม่น้ำทดสอบ" and station["agency"] == "RID"
    assert station["level_msl"] == pytest.approx(4.03) and station["diff_wl_bank"] == pytest.approx(-1.47)
    assert station["bank_msl"] == pytest.approx(5.5)
    assert station["trend_cm_per_hr"] == pytest.approx(1.0, abs=0.01)
    assert station["risk_tier"] in ("normal", "watch") and station["upstream_over"] == []
    assert station["observed_at"].endswith("+07:00")
    shelter = next(s for s in d["sources"] if s["type"] == "shelter")
    assert shelter["name"] == "ศูนย์ทดสอบ" and shelter["patients_ok"] is True     # known, so the card shows it
    assert {"lat", "lon", "district", "is_open", "capacity", "current_residents", "source"} <= set(shelter)
    assert not any(s["type"] == "gistda" for s in d["sources"])        # no flood layer stored: no satellite line
    assert d["emergency"] is False
    assert {q["type"] for q in d["quick_replies"]} >= {"location", "message"}
    assert all(len(q["label"]) <= 20 for q in d["quick_replies"])


def test_location_message_returns_nearest_station(seeded):
    client, _ = seeded
    r = _chat(client, {"session_id": str(uuid.uuid4()), "location": {"lat": 14.41, "lon": 100.46}})
    d = r.json()["data"]
    assert [s["code"] for s in d["sources"] if s["type"] == "station"] == ["T.1"]
    assert "สถานีทดสอบ" in d["reply"]
    assert d["place"]["lat"] == pytest.approx(14.41) and d["place"]["lon"] == pytest.approx(100.46)
    assert d["place"]["approx"] is False


def test_prefixed_duplicate_station_shows_once_with_plain_code_and_river(seeded):
    """ThaiWater lists some RID stations twice, the second as "ridhydro_<code>" with no river: one card, plain code,
    the river taken from the plain-code row."""
    client, conn = seeded
    conn.execute("INSERT INTO stations (id, oldcode, name_th, lat, lon, agency) "
                 "VALUES (9102, 'ridhydro_T.1', 'สถานีทดสอบ', 14.4101, 100.4601, 'RID')")
    for h in range(4):
        conn.execute("INSERT INTO waterlevel_readings (station_id, ts, level_msl, diff_wl_bank) "
                     "VALUES (9102, %s, %s, %s)", (NOW - timedelta(hours=3 - h), 4.0, -1.5))
    risk.compute_all(conn, NOW)
    d = _chat(client, {"session_id": str(uuid.uuid4()), "location": {"lat": 14.41, "lon": 100.46}}).json()["data"]
    stations = [s for s in d["sources"] if s["type"] == "station"]
    assert [s["code"] for s in stations] == ["T.1"] and stations[0]["river"] == "แม่น้ำทดสอบ"
    assert "ridhydro" not in d["reply"]


def test_danger_message_is_emergency_with_what_to_tell_1784(seeded):
    client, _ = seeded
    d = _chat(client, {"session_id": str(uuid.uuid4()),
                       "message": "ช่วยด้วย ติดอยู่บนหลังคาที่บางบาล อยุธยา ยายเดินไม่ได้"}).json()["data"]
    assert d["emergency"] is True and d["quick_replies"] == []
    # EmergencyCard above the reply carries 1784/1669, so the text starts with what NamMa does not do.
    assert chat.RESCUE_HOTLINE not in d["reply"] and not chat._EMOJI.search(d["reply"])
    assert d["reply"].startswith("น้ำมาไม่ได้ส่งเรื่องให้หน่วยกู้ภัยแทน ต้องโทร 1784 แจ้งเอง")
    assert "ตำแหน่ง: อ.บางบาล" in d["reply"] and "ยาย (เดินไม่ได้/ติดเตียง)" in d["reply"]


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
    token = login(client)
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
    assert d["emergency"] is True and "1784" in d["reply"]


def test_rate_limit_is_20_per_minute_per_session(seeded):
    client, _ = seeded
    sid = str(uuid.uuid4())
    for _ in range(20):
        assert _chat(client, {"session_id": sid, "message": "สวัสดี"}).status_code == 200
    r = _chat(client, {"session_id": sid, "message": "สวัสดี"})
    assert r.status_code == 429 and r.json()["error"]["code"] == "rate_limited"
    assert _chat(client, {"session_id": str(uuid.uuid4()), "message": "สวัสดี"}).status_code == 200


def test_shelter_question_lists_shelters_near_the_named_place(seeded):
    client, _ = seeded
    d = _chat(client, {"session_id": str(uuid.uuid4()), "message": "ศูนย์พักพิงแถวบางบาล อยุธยา มีที่ไหนบ้าง"}).json()["data"]
    # The list is the web shelter card and the hotline is HotlineNotice: the text is the heading and the order note.
    assert "บางบาล" in d["reply"] and "ศูนย์ทดสอบ" not in d["reply"] and "1784" not in d["reply"]
    assert "ตามจุดที่ทางการกำหนด" in d["reply"]
    assert [s["type"] for s in d["sources"]] == ["shelter"] and d["sources"][0]["name"] == "ศูนย์ทดสอบ"
    assert d["sources"][0]["distance_km"] is not None and "is_open" in d["sources"][0]
    assert d["emergency"] is False
    assert "บางบาล" in d["place"]["label"]


def test_shelter_question_without_a_place_asks_for_one(seeded):
    client, _ = seeded
    d = _chat(client, {"session_id": str(uuid.uuid4()), "message": "หาศูนย์พักพิง"}).json()["data"]
    assert d["sources"] == [] and "แชร์ตำแหน่ง" in d["reply"] and d["place"] is None
    assert any(q["type"] == "location" for q in d["quick_replies"])


def test_shelter_question_uses_the_saved_place(seeded):
    client, _ = seeded
    sid = str(uuid.uuid4())
    _chat(client, {"session_id": sid, "message": "บ้านแม่อยู่บางบาล อยุธยา น้ำจะถึงไหม"})
    d = _chat(client, {"session_id": sid, "message": "บ้านแม่ควรอพยพไปที่ไหน"}).json()["data"]
    assert "บ้านแม่" in d["reply"] and d["sources"][0]["name"] == "ศูนย์ทดสอบ"


def test_danger_beats_shelter(seeded):
    client, _ = seeded
    d = _chat(client, {"session_id": str(uuid.uuid4()), "message": "ติดอยู่ในบ้าน ไปศูนย์พักพิงไม่ได้ ช่วยด้วย"}).json()["data"]
    assert d["emergency"] is True and d["sources"] == []
