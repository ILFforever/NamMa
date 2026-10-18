import json
from datetime import datetime
from pathlib import Path

from ingest import thaiwater as tw

FIX = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def test_signed_diff_wl_bank():
    assert tw.signed_diff_wl_bank("0.55", "ล้นตลิ่ง (ม.)") == 0.55
    assert tw.signed_diff_wl_bank("0.42", "ต่ำกว่าตลิ่ง (ม.)") == -0.42
    assert tw.signed_diff_wl_bank(None, "ล้นตลิ่ง (ม.)") is None


def test_parse_waterlevel_load():
    stations, readings = tw.parse_waterlevel_load(load("waterlevel_load.json"))
    by_id = {s["id"]: s for s in stations}
    assert by_id[39]["oldcode"] == "CPY011"
    assert by_id[39]["agency"] == "HII"
    assert by_id[2609]["oldcode"] == "C.35"
    assert isinstance(by_id[39]["lat"], float)

    r = {x["station_id"]: x for x in readings}
    assert r[39]["level_msl"] == 4.65                      # string in the API, float here
    assert r[39]["diff_wl_bank"] == -0.42                  # below bank
    assert r[2609]["diff_wl_bank"] == 0.55                 # C.35 over bank on 2026-10-14
    assert r[2609]["discharge"] == 1316.0                  # RID stations carry discharge
    assert r[39]["ts"] == datetime(2026, 10, 14, 15, 30, tzinfo=tw.BKK)


def test_parse_waterlevel_graph_drops_nulls_and_derives_bank_gap():
    rows = tw.parse_waterlevel_graph(39, load("waterlevel_graph_39.json"))
    assert rows and all(r["level_msl"] is not None for r in rows)
    last = rows[-1]
    assert last["ts"] == datetime(2026, 10, 14, 15, 40, tzinfo=tw.BKK)
    assert last["diff_wl_bank"] == round(4.644 - 5.064, 3)


def test_parse_rain_24h():
    stations, readings = tw.parse_rain_24h(load("rain_24h.json"))
    assert len(stations) == len(readings) == 2
    assert readings[0]["rain_24h"] == 149.0


def test_parse_waterlevel_load_drops_future_readings():
    payload = load("waterlevel_load.json")
    now = datetime(2026, 10, 14, 15, 30, tzinfo=tw.BKK)
    _, readings = tw.parse_waterlevel_load(payload, now=now)
    assert 39 in {r["station_id"] for r in readings}              # CPY011 reads 15:30, not in the future

    early = now - tw.timedelta(hours=6)                          # pretend it is 09:30: those readings are bogus
    stations, readings = tw.parse_waterlevel_load(payload, now=early)
    assert all(r["ts"] <= early + tw.FUTURE_TOLERANCE for r in readings)
    assert 39 not in {r["station_id"] for r in readings}
    assert 39 in {s["id"] for s in stations}                      # the station row is still kept
