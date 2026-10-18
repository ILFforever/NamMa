"""DDPM shelters: parsing a trimmed real layer response, the import, ranking in find_shelters, the Thai line."""
import json
from datetime import datetime, timedelta, timezone

from agent import answer, tools
from common.config import ROOT
from ingest import shelters

FIXTURE = json.loads((ROOT / "tests" / "fixtures" / "ddpm_shelters.json").read_text())
RAW = [f["attributes"] for f in FIXTURE["features"]]


def test_parse_keeps_public_located_shelters_and_unknown_stays_unknown():
    out = shelters.parse(RAW)
    # dropped: hidden (is_public_visible=0), deleted, no coordinates
    assert [s["source_code"] for s in out] == ["SH95001746", "SH00015499", "SH00015869"]
    open_, unknown, refuses = out
    assert open_["is_open"] is True and open_["accepts_bedridden"] is True
    assert unknown["is_open"] is False and unknown["accepts_bedridden"] is None    # null is not "no"
    assert refuses["accepts_bedridden"] is False
    assert open_["source_updated_at"].tzinfo is not None
    assert not any(k.startswith("coordinator") or k == "phone" for s in out for k in s)


def _ddpm(code, lat, lon, is_open, bedridden, name=None):
    return {"source_code": code, "name": name or code, "lat": lat, "lon": lon, "capacity": 100, "current_residents": 0,
            "is_open": is_open, "accepts_bedridden": bedridden, "accepts_elderly": None, "accepts_disabled": None,
            "province": "พระนครศรีอยุธยา", "district": "บางบาล", "source_updated_at": None}


def test_store_upserts_and_drops_shelters_missing_from_the_new_import(api):
    _, conn = api
    assert shelters.store(conn, shelters.parse(RAW)) == 3
    assert conn.execute("SELECT count(*) FROM shelters WHERE source = 'ddpm'").fetchone()[0] == 3
    # accepts_patients mirrors a confirmed "yes" only, so the existing ?patients= filter stays truthful
    assert conn.execute("SELECT source_code FROM shelters WHERE accepts_patients").fetchall() == [("SH95001746",)]
    shelters.store(conn, [_ddpm("SH95001746", 14.33, 100.6, False, True, name="renamed")])
    assert conn.execute("SELECT source_code, name, is_open FROM shelters").fetchall() == [("SH95001746", "renamed", False)]
    shelters.store(conn, [])                    # a failed fetch must not wipe the table
    assert conn.execute("SELECT count(*) FROM shelters").fetchone()[0] == 1


def test_import_is_due_daily(api):
    _, conn = api
    assert shelters.due(conn)
    shelters.store(conn, [_ddpm("A", 14.3, 100.5, True, None)])
    assert not shelters.due(conn)
    conn.execute("UPDATE shelters SET imported_at = %s", (datetime.now(timezone.utc) - timedelta(hours=25),))
    assert shelters.due(conn)


def test_find_shelters_prefers_open_and_bedridden_ok_over_slightly_nearer(api):
    """Bang Ban, real case: a closed shelter 1 km away that doesn't say about bedridden patients, vs an open one
    2.8 km away that accepts them. A household with a bedridden member gets the second; others get it too (open)."""
    _, conn = api
    home = (14.38, 100.48)
    shelters.store(conn, [
        _ddpm("near-closed-unknown", 14.389, 100.48, False, None),   # ~1.0 km
        _ddpm("open-bedridden", 14.405, 100.48, True, True),         # ~2.8 km
        _ddpm("open-refuses", 14.39, 100.48, True, False),           # ~1.1 km
    ])
    care = tools.find_shelters(conn, *home, {"patients": True}, k=3)
    assert [s["source_code"] for s in care] == ["open-bedridden", "near-closed-unknown", "open-refuses"]
    assert care[0]["patients_ok"] is True and care[0]["distance_km"] == 2.8
    anyone = tools.find_shelters(conn, *home, k=3)
    assert [s["source_code"] for s in anyone] == ["open-refuses", "open-bedridden", "near-closed-unknown"]


def test_find_shelters_always_returns_something(api):
    _, conn = api
    shelters.store(conn, [_ddpm("far-refuses", 16.0, 100.48, False, False)])     # ~180 km, outside the search box
    got = tools.find_shelters(conn, 14.38, 100.48, {"patients": True, "pets": True})
    assert [s["source_code"] for s in got] == ["far-refuses"]


def test_shelter_line_says_status_and_unknown_and_credits_ddpm():
    s = {"name": "วัดโบสถ์", "district": "บางบาล", "distance_km": 1.0, "is_open": False, "patients_ok": None,
         "source": "ddpm", "capacity": 100, "current_residents": 0}
    lines = answer.shelter_line([s], need_patients=True)
    assert lines[0] == "🏫 ศูนย์พักพิงที่แนะนำ: วัดโบสถ์ อ.บางบาล ห่าง 1.0 กม. (ยังไม่เปิด, ไม่ได้ระบุว่ารับผู้ป่วยติดเตียง)"
    assert "ปภ." in lines[1] and "1784" in lines[1]
    s.update(is_open=True, patients_ok=True, current_residents=12)
    assert "(เปิดรับอยู่ มีผู้พัก 12/100 คน, รับผู้ป่วยติดเตียง)" in answer.shelter_line([s])[0]
    # unknown is only mentioned when the household needs it
    s.update(patients_ok=None)
    assert "ติดเตียง" not in answer.shelter_line([s])[0]
