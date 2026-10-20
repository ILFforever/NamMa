"""GET/DELETE /api/profile (docs/api.md → Profile) against a real Postgres: what a web chat session saved.

Skipped when no Postgres is reachable (see the api fixture in conftest). Network calls are disabled.
"""
import uuid

from tests.conftest import login
from tests.test_api_chat import STATION, _chat, seeded  # noqa: F401  (seeded is a fixture)


def _profile(client, sid: str | None = None, token: str | None = None, method: str = "GET", path: str = "",
             json: dict | None = None):
    headers = {}
    if sid:
        headers["X-Chat-Session"] = sid
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return client.request(method, "/api/profile" + path, headers=headers, json=json)


def test_unknown_session_gets_an_empty_profile_and_creates_no_user(seeded):
    client, conn = seeded
    before = conn.execute("SELECT count(*) FROM users").fetchone()[0]
    for sid in (None, str(uuid.uuid4())):
        r = _profile(client, sid)
        assert r.status_code == 200
        d = r.json()["data"]
        assert d == {"places": [], "facts": [], "consent": {"granted": False, "ts": None},
                     "pending_consent": False, "line_linked": False}
    assert conn.execute("SELECT count(*) FROM users").fetchone()[0] == before


def test_profile_shows_the_saved_place_with_its_nearest_station(seeded):
    client, _ = seeded
    sid = str(uuid.uuid4())
    _chat(client, {"session_id": sid, "message": "บ้านแม่อยู่บางบาล อยุธยา น้ำจะถึงไหม"})
    d = _profile(client, sid).json()["data"]
    [place] = d["places"]
    assert place["label"] == "บ้านแม่" and place["approx"] is True
    assert "บางบาล" in place["where"]
    assert place["nearest"]["station_id"] == STATION and place["nearest"]["code"] == "T.1"
    assert place["nearest"]["risk_tier"] in ("normal", "watch") and place["nearest"]["distance_km"] > 0


def test_sensitive_fact_shows_only_after_consent(seeded):
    client, _ = seeded
    sid = str(uuid.uuid4())
    _chat(client, {"session_id": sid, "message": "ยายติดเตียง บ้านสองชั้น"})
    d = _profile(client, sid).json()["data"]
    assert d["pending_consent"] is True
    assert [f["kind"] for f in d["facts"]] == ["house"]          # house is not sensitive: saved at once

    _chat(client, {"session_id": sid, "postback": "consent:sensitive_memory:yes"})
    d = _profile(client, sid).json()["data"]
    assert d["pending_consent"] is False and d["consent"]["granted"] is True and d["consent"]["ts"]
    hh = [f for f in d["facts"] if f["kind"] == "household"]
    assert hh and hh[0]["sensitive"] is True and "ยาย" in hh[0]["text"]


def test_delete_a_place_and_facts(seeded):
    client, _ = seeded
    sid = str(uuid.uuid4())
    _chat(client, {"session_id": sid, "message": "บ้านแม่อยู่บางบาล อยุธยา น้ำจะถึงไหม"})
    _chat(client, {"session_id": sid, "message": "บ้านสองชั้น"})
    d = _profile(client, sid).json()["data"]
    pid = d["places"][0]["id"]

    r = _profile(client, sid, method="DELETE", path=f"/places/{pid}")
    assert r.status_code == 200 and r.json()["data"]["places"] == []
    assert _profile(client, sid, method="DELETE", path=f"/places/{pid}").status_code == 404

    r = _profile(client, sid, method="DELETE", path="/facts/house")
    assert r.status_code == 200 and r.json()["data"]["facts"] == []
    assert _profile(client, sid, method="DELETE", path="/facts/everything").status_code == 400


def test_another_session_cannot_delete_my_place(seeded):
    client, _ = seeded
    mine, theirs = str(uuid.uuid4()), str(uuid.uuid4())
    _chat(client, {"session_id": mine, "message": "บ้านแม่อยู่บางบาล อยุธยา น้ำจะถึงไหม"})
    pid = _profile(client, mine).json()["data"]["places"][0]["id"]
    assert _profile(client, theirs, method="DELETE", path=f"/places/{pid}").status_code == 404
    assert _profile(client, mine).json()["data"]["places"][0]["id"] == pid


def test_rename_a_place(seeded):
    client, _ = seeded
    sid, theirs = str(uuid.uuid4()), str(uuid.uuid4())
    _chat(client, {"session_id": sid, "message": "บ้านแม่อยู่บางบาล อยุธยา น้ำจะถึงไหม"})
    _chat(client, {"session_id": sid, "message": "บ้านอยู่บางบาล อยุธยา"})
    places = _profile(client, sid).json()["data"]["places"]
    pid = next(p["id"] for p in places if p["label"] == "บ้านแม่")

    r = _profile(client, sid, method="PATCH", path=f"/places/{pid}", json={"label": "  บ้านคุณแม่ "})
    assert r.status_code == 200
    assert {p["label"] for p in r.json()["data"]["places"]} == {"บ้านคุณแม่", "บ้าน"}

    assert _profile(client, sid, method="PATCH", path=f"/places/{pid}", json={"label": "บ้าน"}).status_code == 409
    assert _profile(client, sid, method="PATCH", path=f"/places/{pid}", json={"label": "  "}).status_code == 400
    assert _profile(client, theirs, method="PATCH", path=f"/places/{pid}", json={"label": "x"}).status_code == 404


def test_rename_a_name_in_memory(seeded):
    client, _ = seeded
    sid = str(uuid.uuid4())
    _chat(client, {"session_id": sid, "message": "ยายติดเตียง บ้านสองชั้น"})
    _chat(client, {"session_id": sid, "postback": "consent:sensitive_memory:yes"})
    facts = _profile(client, sid).json()["data"]["facts"]
    [hh] = [f for f in facts if f["list"] == "immobile"]
    assert hh["names"] == ["ยาย"]
    assert all(f["list"] is None and f["names"] == [] for f in facts if f["kind"] == "house")

    body = {"list": "immobile", "from": "ยาย", "to": "ยายสมศรี"}
    r = _profile(client, sid, method="PATCH", path="/names", json=body)
    assert r.status_code == 200
    [hh] = [f for f in r.json()["data"]["facts"] if f["list"] == "immobile"]
    assert hh["names"] == ["ยายสมศรี"] and "ยายสมศรี" in hh["text"]

    assert _profile(client, sid, method="PATCH", path="/names", json=body).status_code == 404       # ยาย is gone
    assert _profile(client, sid, method="PATCH", path="/names",
                    json={**body, "list": "car"}).status_code == 400
    assert _profile(client, str(uuid.uuid4()), method="PATCH", path="/names",
                    json={**body, "from": "ยายสมศรี"}).status_code == 404


def test_signed_in_user_is_line_linked(seeded):
    client, _ = seeded
    token = login(client, "user")
    d = _profile(client, token=token).json()["data"]
    assert d["line_linked"] is True and d["places"] == []


def test_a_place_has_its_own_facts(seeded):
    """places[].facts: what holds at that place only. Deleting and renaming there leave the general facts alone."""
    client, _ = seeded
    sid = str(uuid.uuid4())
    _chat(client, {"session_id": sid, "message": "ร้านอยู่บางบาล อยุธยา น้ำจะถึงไหม"})
    _chat(client, {"session_id": sid, "message": "ร้านมีหมา"})
    d = _profile(client, sid).json()["data"]
    assert d["pending_consent"] is True and d["places"][0]["facts"] == []     # waits for consent, at the place
    _chat(client, {"session_id": sid, "postback": "consent:sensitive_memory:yes"})
    _chat(client, {"session_id": sid, "message": "มีรถยนต์"})                  # general
    d = _profile(client, sid).json()["data"]
    [shop] = d["places"]
    [pets] = shop["facts"]
    assert pets == {"kind": "household", "text": "สัตว์เลี้ยง: หมา", "sensitive": True, "list": "pets", "names": ["หมา"]}
    assert [f["text"] for f in d["facts"]] == ["มีรถยนต์"]

    body = {"list": "pets", "from": "หมา", "to": "หมาโบ้", "place": shop["id"]}
    r = _profile(client, sid, method="PATCH", path="/names", json=body)
    assert r.status_code == 200 and r.json()["data"]["places"][0]["facts"][0]["names"] == ["หมาโบ้"]
    assert _profile(client, str(uuid.uuid4()), method="PATCH", path="/names", json=body).status_code == 404

    r = _profile(client, sid, method="DELETE", path="/facts/household")       # the general ones: the dog stays
    assert r.json()["data"]["places"][0]["facts"] != []
    assert _profile(client, sid, method="DELETE", path=f"/places/{shop['id']}/facts/needs").status_code == 400
    r = _profile(client, sid, method="DELETE", path=f"/places/{shop['id']}/facts/household")
    assert r.status_code == 200 and r.json()["data"]["places"][0]["facts"] == []
    assert r.json()["data"]["facts"] != []                                    # the car stays
    other = str(uuid.uuid4())
    assert _profile(client, other, method="DELETE", path=f"/places/{shop['id']}/facts/house").status_code == 404
