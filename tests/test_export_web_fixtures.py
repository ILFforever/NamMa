from datetime import datetime, timedelta

from ingest.export_web_fixtures import hourly, provisional_risk, trend_cm_per_hr
from ingest.thaiwater import BKK

T0 = datetime(2026, 10, 14, 12, 0, tzinfo=BKK)


def test_provisional_risk_tiers():
    assert provisional_risk(None, 5.0, False) is None
    assert provisional_risk(0.15, -2.0, False) == ("danger", None)          # over bank, even if falling
    assert provisional_risk(-0.20, 5.0, False) == ("warning", 4.0)          # 20 cm at 5 cm/h → 4 h
    assert provisional_risk(-2.0, -1.0, True)[0] == "warning"               # upstream already over bank
    assert provisional_risk(-0.40, 1.0, False) == ("watch", 40.0)           # rising, < 0.5 m to bank
    assert provisional_risk(-0.40, 0.2, False)[0] == "normal"               # flat counts as not rising
    assert provisional_risk(-1.50, 3.0, False) == ("normal", 50.0)


def test_trend_cm_per_hr():
    rows = [{"ts": T0 + timedelta(hours=h), "level_msl": 4.0 + 0.03 * h} for h in range(5)]
    assert trend_cm_per_hr(rows) == 3.0
    assert trend_cm_per_hr(rows[:1]) is None
    # gap: nothing near 3 h before the latest reading
    assert trend_cm_per_hr([rows[0], {"ts": T0 + timedelta(hours=10), "level_msl": 5.0}]) is None


def test_hourly_keeps_first_reading_per_hour():
    rows = [{"ts": T0 + timedelta(minutes=m), "level_msl": m} for m in (0, 10, 20, 60, 70)]
    assert [r["level_msl"] for r in hourly(rows)] == [0, 60]


def test_river_fixtures_keeps_corridor_summaries_and_adds_other_rivers():
    from ingest.export_web_fixtures import river_fixtures

    def s(sid, river, lat, bank, agency="RID"):
        return {"id": sid, "oldcode": f"S{sid}", "name_th": f"สถานี {sid}", "name_en": None, "lat": lat, "lon": 100.0,
                "river": river, "province": "จ", "amphoe": None, "agency": agency, "is_key_station": True,
                "basin": "ลุ่ม", "min_bank": bank}
    all_stations = [s(1, "แม่น้ำเจ้าพระยา", 15.6, 25.0), s(2, "แม่น้ำเจ้าพระยา", 14.4, 4.0),
                    s(3, "แม่น้ำเจ้าพระยา", 14.40001, 5.0, agency="HII"),       # same place as 2
                    s(10, "แม่น้ำยม", 17.0, 50.0), s(11, "แม่น้ำยม", 16.5, 38.0), s(20, "คลองเดี่ยว", 13.0, 3.0)]
    latest = {sid: {"station_id": sid, "ts": T0, "level_msl": 1.0, "diff_wl_bank": d, "situation_level": None,
                    "discharge": None} for sid, d in ((1, -2.0), (2, 0.1), (3, 0.2), (10, 0.3), (11, -1.0))}
    corridor = {1: {"id": 1, "marker": "corridor", "stem_order": 1, "role": "main", "latest": None, "risk": None,
                    **{k: None for k in ("oldcode", "name_th", "name_en", "lat", "lon", "river", "province", "amphoe",
                                         "agency", "is_key_station")}}}
    out = river_fixtures(all_stations, latest, corridor, T0)
    assert [(r["key"], [x["id"] for x in r["stations"]]) for r in out] == [("แม่น้ำเจ้าพระยา", [1, 2]), ("แม่น้ำยม", [10, 11])]
    assert out[0]["stations"][0]["stem_order"] == 1                      # the corridor's own summary
    assert out[1]["stations"][0]["risk"]["tier"] == "danger"           # over bank, provisional
    assert out[1]["stations"][0]["stem_order"] is None and out[1]["provinces"] == ["จ"]
