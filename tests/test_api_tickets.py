"""Rescue ticket endpoints (docs/api.md → Rescue tickets): auth, list, detail + audit, PATCH, vocabulary."""
import re
from datetime import datetime, timedelta, timezone

from agent import tools
from tests.conftest import login

SUMMARY_KEYS = {"id", "place_label", "lat", "lon", "people_count", "vulnerable", "needs", "urgency", "status",
                "channel", "created_at", "updated_at"}
PDPA_KEYS = {"raw_text", "contact_phone", "assignee", "notes"}
T0 = datetime(2026, 10, 17, 8, 0, tzinfo=timezone.utc)


def seed(conn, **kw) -> int:
    fields = dict(user_id=None, raw_text="ช่วยด้วย น้ำเข้าบ้าน", lat=14.369, lon=100.474, people_count=None,
                  vulnerable=[], needs=[], urgency="high", status="open", channel="line", place_label="อ.บางบาล")
    fields.update(kw)
    return tools.create_rescue_ticket(conn, **fields)


def set_times(conn, tid: int, created_min: int, updated_min: int | None = None) -> None:
    updated = T0 + timedelta(minutes=created_min if updated_min is None else updated_min)
    conn.execute("UPDATE rescue_tickets SET created_at = %s, updated_at = %s WHERE id = %s",
                 (T0 + timedelta(minutes=created_min), updated, tid))


def auth(client, role: str = "responder") -> dict:
    return {"Authorization": f"Bearer {login(client, role)}"}


# --- auth ---------------------------------------------------------------------------------------

def test_tickets_need_a_session(api):
    client, _ = api
    r = client.get("/api/tickets")
    assert r.status_code == 401 and r.json()["error"]["code"] == "unauthorized"
    r = client.get("/api/tickets", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_token"


def test_non_responder_gets_403_on_every_ticket_route(api):
    client, conn = api
    tid = seed(conn)
    h = auth(client, "user")
    assert client.get("/api/tickets", headers=h).json()["error"]["code"] == "forbidden"
    assert client.get(f"/api/tickets/{tid}", headers=h).status_code == 403
    r = client.patch(f"/api/tickets/{tid}", headers=h, json={"status": "resolved", "updated_at": "2026-10-17T08:00:00Z"})
    assert r.status_code == 403
    assert conn.execute("SELECT status FROM rescue_tickets WHERE id = %s", (tid,)).fetchone()[0] == "open"


# --- list ---------------------------------------------------------------------------------------

def test_list_hides_unconfirmed_and_omits_pdpa_fields(api):
    client, conn = api
    seed(conn, status="unconfirmed", raw_text="ยังไม่ยืนยัน")
    tid = seed(conn, status="open", vulnerable=["ผู้สูงอายุ"], needs=["boat"], people_count=3)
    conn.execute("UPDATE rescue_tickets SET contact_phone = '0812345678', assignee = 'ทีม A', notes = 'x' WHERE id = %s",
                 (tid,))
    r = client.get("/api/tickets", headers=auth(client))
    assert r.status_code == 200
    body = r.json()
    assert [t["id"] for t in body["data"]] == [tid]
    ticket = body["data"][0]
    assert set(ticket) == SUMMARY_KEYS and not (PDPA_KEYS & set(ticket))
    assert ticket["vulnerable"] == ["elderly"] and ticket["needs"] == ["boat"] and ticket["people_count"] == 3
    assert ticket["created_at"].endswith("+07:00") and ticket["updated_at"].endswith("+07:00")
    assert set(body["meta"]) == {"ingested_at"}


def test_list_sorted_by_urgency_then_age(api):
    client, conn = api
    normal_old = seed(conn, urgency="normal")
    critical_new = seed(conn, urgency="critical")
    high = seed(conn, urgency="high")
    critical_old = seed(conn, urgency="critical")
    set_times(conn, normal_old, 0)
    set_times(conn, critical_new, 30)
    set_times(conn, high, 10)
    set_times(conn, critical_old, 5)
    ids = [t["id"] for t in client.get("/api/tickets", headers=auth(client)).json()["data"]]
    assert ids == [critical_old, critical_new, high, normal_old]


def test_list_status_filter(api):
    client, conn = api
    opened = seed(conn, status="open")
    acked = seed(conn, status="acknowledged")
    done = seed(conn, status="resolved")
    h = auth(client)

    def ids(url):
        return sorted(t["id"] for t in client.get(url, headers=h).json()["data"])

    assert ids("/api/tickets?status=open,acknowledged") == sorted([opened, acked])
    assert ids("/api/tickets?status=open") == [opened]
    assert ids("/api/tickets") == sorted([opened, acked, done])          # everything except unconfirmed


def test_list_rejects_unknown_status(api):
    client, _ = api
    h = auth(client)
    for q in ("status=open,bogus", "status=unconfirmed", "status=done"):
        r = client.get(f"/api/tickets?{q}", headers=h)
        assert r.status_code == 400 and r.json()["error"]["code"] == "bad_request", q


def test_list_since_uses_updated_at(api):
    client, conn = api
    old = seed(conn)
    new = seed(conn)
    set_times(conn, old, 0, updated_min=0)
    set_times(conn, new, 0, updated_min=20)
    h = auth(client)
    since = (T0 + timedelta(minutes=10)).isoformat()
    assert [t["id"] for t in client.get("/api/tickets", params={"since": since}, headers=h).json()["data"]] == [new]
    r = client.get("/api/tickets?since=yesterday", headers=h)
    assert r.status_code == 400 and r.json()["error"]["code"] == "bad_request"


# --- detail -------------------------------------------------------------------------------------

def test_detail_returns_full_ticket_and_records_a_view(api):
    client, conn = api
    tid = seed(conn, raw_text="ยายติดเตียง อยู่ชั้นสอง", channel="web")
    conn.execute("UPDATE rescue_tickets SET contact_phone = '0812345678', notes = 'โทรแล้ว' WHERE id = %s", (tid,))
    h = auth(client)
    uid = conn.execute("SELECT id FROM users WHERE role = 'responder'").fetchone()[0]
    r = client.get(f"/api/tickets/{tid}", headers=h)
    assert r.status_code == 200
    t = r.json()["data"]
    assert set(t) == SUMMARY_KEYS | PDPA_KEYS
    assert t["raw_text"] == "ยายติดเตียง อยู่ชั้นสอง" and t["contact_phone"] == "0812345678"
    assert t["notes"] == "โทรแล้ว" and t["channel"] == "web" and t["assignee"] is None
    events = conn.execute("SELECT actor, action FROM ticket_events WHERE ticket_id = %s", (tid,)).fetchall()
    assert events == [(str(uid), "view")]


def test_detail_404_for_missing_and_unconfirmed(api):
    client, conn = api
    unconfirmed = seed(conn, status="unconfirmed")
    h = auth(client)
    for tid in (unconfirmed, 999999):
        r = client.get(f"/api/tickets/{tid}", headers=h)
        assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"
    assert conn.execute("SELECT count(*) FROM ticket_events").fetchone()[0] == 0


# --- PATCH --------------------------------------------------------------------------------------

def _updated_at(conn, tid: int) -> str:
    """What the client last saw: read from the DB, so GETs do not add view events to the audit trail."""
    return conn.execute("SELECT updated_at FROM rescue_tickets WHERE id = %s", (tid,)).fetchone()[0].isoformat()


def test_patch_status_and_assignee_write_audit_and_bump_updated_at(api):
    client, conn = api
    tid = seed(conn)
    h = auth(client)
    before = _updated_at(conn, tid)
    r = client.patch(f"/api/tickets/{tid}", headers=h,
                     json={"status": "acknowledged", "assignee": "ทีม A", "updated_at": before})
    assert r.status_code == 200
    t = r.json()["data"]
    assert t["status"] == "acknowledged" and t["assignee"] == "ทีม A" and t["notes"] is None
    assert t["updated_at"] != before and set(t) == SUMMARY_KEYS | PDPA_KEYS
    rows = conn.execute("SELECT action, detail FROM ticket_events WHERE ticket_id = %s ORDER BY id", (tid,)).fetchall()
    assert [a for a, _ in rows] == ["status", "assign"]
    assert rows[0][1] == {"from": "open", "to": "acknowledged"}
    assert rows[1][1] == {"from": None, "to": "ทีม A"}


def test_patch_note_is_appended_with_time_and_name(api):
    client, conn = api
    tid = seed(conn)
    conn.execute("UPDATE rescue_tickets SET notes = 'โทรหาแล้ว' WHERE id = %s", (tid,))
    h = auth(client)
    first = client.patch(f"/api/tickets/{tid}", headers=h,
                         json={"note": "ส่งเรือไปแล้ว", "updated_at": _updated_at(conn, tid)})
    assert first.status_code == 200
    second = client.patch(f"/api/tickets/{tid}", headers=h,
                          json={"note": "ถึงแล้ว", "updated_at": first.json()["data"]["updated_at"]})
    notes = second.json()["data"]["notes"].split("\n")
    assert notes[0] == "โทรหาแล้ว"
    assert re.fullmatch(r"\d{2}:\d{2} เจ้าหน้าที่ทดลอง: ส่งเรือไปแล้ว", notes[1])
    assert re.fullmatch(r"\d{2}:\d{2} เจ้าหน้าที่ทดลอง: ถึงแล้ว", notes[2])
    actions = [a for (a,) in conn.execute("SELECT action FROM ticket_events WHERE ticket_id = %s ORDER BY id", (tid,))]
    assert actions == ["note", "note"]


def test_patch_with_stale_updated_at_is_409_and_changes_nothing(api):
    client, conn = api
    tid = seed(conn)
    h = auth(client)
    stale = _updated_at(conn, tid)
    assert client.patch(f"/api/tickets/{tid}", headers=h,
                        json={"status": "in_progress", "updated_at": stale}).status_code == 200
    r = client.patch(f"/api/tickets/{tid}", headers=h, json={"status": "resolved", "updated_at": stale})
    assert r.status_code == 409 and r.json()["error"]["code"] == "conflict"
    assert conn.execute("SELECT status FROM rescue_tickets WHERE id = %s", (tid,)).fetchone()[0] == "in_progress"


def test_patch_compares_updated_at_as_a_timestamp(api):
    client, conn = api
    tid = seed(conn)
    h = auth(client)
    current = datetime.fromisoformat(_updated_at(conn, tid))
    same_instant_utc = current.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    r = client.patch(f"/api/tickets/{tid}", headers=h, json={"note": "ok", "updated_at": same_instant_utc})
    assert r.status_code == 200


def test_patch_bad_status_is_400(api):
    client, conn = api
    tid = seed(conn)
    h = auth(client)
    stamp = _updated_at(conn, tid)
    for bad in ("unconfirmed", "done", "assigned"):
        r = client.patch(f"/api/tickets/{tid}", headers=h, json={"status": bad, "updated_at": stamp})
        assert r.status_code == 400 and r.json()["error"]["code"] == "bad_request", bad
    assert conn.execute("SELECT status FROM rescue_tickets WHERE id = %s", (tid,)).fetchone()[0] == "open"


def test_patch_unconfirmed_or_missing_is_404(api):
    client, conn = api
    tid = seed(conn, status="unconfirmed")
    h = auth(client)
    stamp = "2026-10-17T08:00:00+07:00"
    assert client.patch(f"/api/tickets/{tid}", headers=h, json={"status": "open", "updated_at": stamp}).status_code == 404
    assert client.patch("/api/tickets/999999", headers=h, json={"note": "x", "updated_at": stamp}).status_code == 404


def test_patch_requires_updated_at(api):
    client, conn = api
    tid = seed(conn)
    r = client.patch(f"/api/tickets/{tid}", headers=auth(client), json={"status": "resolved"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "bad_request"


# --- vocabulary ---------------------------------------------------------------------------------

def test_stored_vocabulary_maps_to_contract_enums(api):
    client, conn = api
    tid = seed(conn,
               vulnerable=["children", "ยาย (เดินไม่ได้/ติดเตียง)", "ผู้สูงอายุ", "เด็กเล็ก", "หญิงตั้งครรภ์",
                           "ต้องฟอกไต", "ทารกแรกเกิด", "something unknown"],
               needs=["food", "drinking_water", "medicine", "transport", "boat", "oxygen"],
               urgency="critical")
    t = client.get(f"/api/tickets/{tid}", headers=auth(client)).json()["data"]
    assert t["vulnerable"] == ["elderly", "bedridden", "child", "infant", "pregnant"]
    assert t["needs"] == ["medication", "oxygen", "dialysis", "food_water", "boat"]
    assert t["urgency"] == "critical"


def test_unknown_urgency_is_high():
    from api import tickets
    assert tickets._urgency("critical") == "critical"
    assert tickets._urgency("weird") == "high"
    assert tickets._urgency(None) == "high"
