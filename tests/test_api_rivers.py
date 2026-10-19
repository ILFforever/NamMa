"""GET /api/rivers and /api/rivers/{key} (docs/api.md → Rivers) against a real Postgres, grouped by ingest.rivers."""
from datetime import datetime, timedelta, timezone

from ingest import rivers

RIVER_KEYS = {"key", "name_th", "basins", "provinces", "worst_tier", "reason", "distance_km", "stations"}


def _station(conn, sid, river, lat, lon, bank, *, agency="RID", province="นครสวรรค์", basin="ลุ่มน้ำเจ้าพระยา"):
    conn.execute(
        "INSERT INTO stations (id, oldcode, name_th, lat, lon, basin, river, province, min_bank, agency) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (sid, f"S{sid}", f"สถานี {sid}", lat, lon, basin, river, province, bank, agency))
    conn.execute("INSERT INTO waterlevel_readings (station_id, ts, level_msl, diff_wl_bank) VALUES (%s, %s, %s, %s)",
                 (sid, datetime.now(timezone.utc) - timedelta(minutes=30), bank - 1, -1.0))


def _risk(conn, sid, tier, diff):
    conn.execute("UPDATE waterlevel_readings SET diff_wl_bank = %s WHERE station_id = %s", (diff, sid))
    conn.execute("INSERT INTO station_risk (station_id, computed_at, level, risk_tier, forecast_json) "
                 "VALUES (%s, now(), 1, %s, '{}')", (sid, tier))


def _seed(conn):
    # Chao Phraya: three places, one gauged twice (2 and 3 are 10 m apart).
    _station(conn, 1, "แม่น้ำเจ้าพระยา", 15.67, 100.11, 25.7)
    _station(conn, 2, "แม่น้ำเจ้าพระยา", 14.3691, 100.5768, 4.35, province="พระนครศรีอยุธยา")
    _station(conn, 3, "แม่น้ำเจ้าพระยา", 14.36913, 100.5769, 5.06, agency="HII", province="พระนครศรีอยุธยา")
    _station(conn, 4, "แม่น้ำเจ้าพระยา", 15.0, 100.3, 13.2, province="สิงห์บุรี")
    # Yom: two stations. Mun: two stations. Khlong Bang Ban: one, so never a river of its own.
    _station(conn, 10, "แม่น้ำยม", 17.0, 99.8, 50, province="สุโขทัย", basin="ลุ่มน้ำยม")
    _station(conn, 11, "แม่น้ำยม", 16.4, 100.2, 38, province="พิษณุโลก", basin="ลุ่มน้ำยม")
    _station(conn, 20, "แม่น้ำมูล", 15.2, 103.0, 150, province="บุรีรัมย์", basin="ลุ่มน้ำมูล")
    _station(conn, 21, "แม่น้ำมูล", 15.3, 104.1, 112, province="อุบลราชธานี", basin="ลุ่มน้ำมูล")
    _station(conn, 30, "คลองบางบาล", 14.37, 100.48, 4.0, province="พระนครศรีอยุธยา")
    assert rivers.run_once(conn) == 9


def test_detail_is_upstream_first_without_the_second_gauge_at_one_place(api):
    client, conn = api
    _seed(conn)
    r = client.get("/api/rivers/แม่น้ำเจ้าพระยา")
    assert r.status_code == 200
    river = r.json()["data"]
    assert set(river) == RIVER_KEYS
    assert [s["id"] for s in river["stations"]] == [1, 4, 2]
    assert river["provinces"] == ["นครสวรรค์", "สิงห์บุรี", "พระนครศรีอยุธยา"]
    assert river["basins"] == ["ลุ่มน้ำเจ้าพระยา"]
    assert river["stations"][0]["latest"]["diff_wl_bank"] == -1.0
    assert conn.execute("SELECT same_place_as FROM stations WHERE id = 3").fetchone()[0] == 2


def test_unknown_or_single_station_river_is_404(api):
    client, conn = api
    _seed(conn)
    assert client.get("/api/rivers/คลองบางบาล").status_code == 404
    assert client.get("/api/rivers/nope").json()["error"]["code"] == "not_found"


def test_list_puts_at_risk_rivers_worst_first_then_the_one_near_you(api):
    client, conn = api
    _seed(conn)
    _risk(conn, 10, "watch", -0.3)
    _risk(conn, 2, "danger", 0.4)
    data = client.get("/api/rivers", params={"lat": 15.25, "lon": 104.0}).json()["data"]
    assert [(d["key"], d["reason"]) for d in data] == [
        ("แม่น้ำเจ้าพระยา", "at_risk"), ("แม่น้ำยม", "at_risk"), ("แม่น้ำมูล", "near_you")]
    assert data[0]["worst_tier"] == "danger" and data[2]["worst_tier"] is None
    assert data[2]["distance_km"] is not None and data[2]["distance_km"] < 15


def test_list_with_nothing_at_risk_and_no_place_falls_back_to_best_gauged(api):
    client, conn = api
    _seed(conn)
    data = client.get("/api/rivers").json()["data"]
    assert [(d["key"], d["reason"]) for d in data][0] == ("แม่น้ำเจ้าพระยา", "largest")
    assert len(data) == 3 and all(d["distance_km"] is None for d in data)


def test_search_by_name(api):
    client, conn = api
    _seed(conn)
    data = client.get("/api/rivers", params={"q": "ยม"}).json()["data"]
    assert [(d["key"], d["reason"]) for d in data] == [("แม่น้ำยม", "match")]
    assert client.get("/api/rivers", params={"q": "ไม่มี"}).json()["data"] == []


def test_lat_without_lon_is_rejected(api):
    client, _ = api
    r = client.get("/api/rivers", params={"lat": 14})
    assert r.status_code == 400 and r.json()["error"]["code"] == "bad_request"
