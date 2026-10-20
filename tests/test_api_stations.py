"""Stations and shelters endpoints (docs/api.md → Stations, Shelters) against a real Postgres."""
from datetime import datetime, timedelta, timezone

from common import seed

STATION_SUMMARY_KEYS = {"id", "oldcode", "name_th", "name_en", "lat", "lon", "river", "province", "amphoe",
                        "agency", "is_key_station", "stem_order", "role", "same_place_as", "latest", "risk"}
DETAIL_KEYS = STATION_SUMMARY_KEYS | {"left_bank", "right_bank", "min_bank", "ground_level",
                                      "critical_level_msl", "upstream", "downstream"}
SHELTER_KEYS = {"id", "name", "lat", "lon", "capacity", "accepts_patients", "accepts_pets", "phone", "distance_km",
                "is_open", "current_residents", "accepts_bedridden", "province", "district", "source", "updated_at"}


def _station(conn, sid, *, lat=14.0, lon=100.0, stem_order=None, role=None, oldcode=None, **extra):
    conn.execute(
        "INSERT INTO stations (id, oldcode, name_th, name_en, lat, lon, river, province, amphoe, agency, "
        "is_key_station, stem_order, role, left_bank, right_bank, min_bank, ground_level, critical_level_msl) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (sid, oldcode or f"S{sid}", f"สถานี {sid}", f"Station {sid}", lat, lon, "แม่น้ำเจ้าพระยา",
         "Ayutthaya", "Bang Pa-in", "RID", True, stem_order, role,
         extra.get("left_bank"), extra.get("right_bank"), extra.get("min_bank"),
         extra.get("ground_level"), extra.get("critical_level_msl")))


def _reading(conn, sid, ts, level, diff=None, discharge=None, situation=None):
    conn.execute("INSERT INTO waterlevel_readings (station_id, ts, level_msl, diff_wl_bank, situation_level, discharge) "
                 "VALUES (%s, %s, %s, %s, %s, %s)", (sid, ts, level, diff, situation, discharge))


def _risk(conn, sid, tier, computed_at, trend=-0.67, hours=None):
    conn.execute("INSERT INTO station_risk (station_id, computed_at, level, trend_cm_per_hr, hours_to_bank, risk_tier, "
                 "forecast_json) VALUES (%s, %s, 4.69, %s, %s, %s, '{}')", (sid, computed_at, trend, hours, tier))


BKK = timezone(timedelta(hours=7))


def _iso(dt: datetime) -> str:
    """How the API prints a timestamp: Bangkok time with +07:00 (the session time zone)."""
    return dt.astimezone(BKK).isoformat()


def _hour(hours_ago: int, minute: int = 0) -> datetime:
    """A Bangkok-aligned timestamp `hours_ago` hours back (whole hours, so hourly buckets are predictable)."""
    base = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    return base - timedelta(hours=hours_ago) + timedelta(minutes=minute)


# --- GET /api/stations ---------------------------------------------------------------------------

def test_list_shows_corridor_with_latest_and_risk(api):
    client, conn = api
    now = datetime.now(timezone.utc)
    _station(conn, 1, stem_order=1, role="main", oldcode="C.2")
    _station(conn, 2, role="branch", oldcode="C.36")         # off the main stem: stem_order null, role set
    _station(conn, 3, oldcode="X.1")                          # not on the corridor, stem_order and role both null
    _reading(conn, 1, _hour(5), 4.1, 0.2, 900.0, 3)
    _reading(conn, 1, _hour(1), 4.69, 0.34, 1254.0, 5)
    _risk(conn, 1, "danger", now, trend=-0.67, hours=None)

    r = client.get("/api/stations")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"data", "meta"}
    assert [s["id"] for s in body["data"]] == [1, 2]          # main stem first, then the branch
    s1 = body["data"][0]
    assert set(s1) == STATION_SUMMARY_KEYS
    assert (s1["stem_order"], s1["role"], s1["oldcode"]) == (1, "main", "C.2")
    assert s1["latest"] == {"ts": _iso(_hour(1)), "level_msl": 4.69, "diff_wl_bank": 0.34,
                            "situation_level": 5, "discharge": 1254.0}
    assert set(s1["risk"]) == {"tier", "trend_cm_per_hr", "hours_to_bank", "computed_at"}
    assert s1["risk"]["tier"] == "danger" and s1["risk"]["trend_cm_per_hr"] == -0.67
    assert body["data"][1]["latest"] is None and body["data"][1]["risk"] is None
    assert body["data"][1]["role"] == "branch" and body["data"][1]["stem_order"] is None


def test_latest_is_the_newest_reading_not_the_last_inserted(api):
    client, conn = api
    _station(conn, 1, stem_order=1, role="main")
    _reading(conn, 1, _hour(1), 4.69)
    _reading(conn, 1, _hour(6), 3.0)        # inserted last, but older
    _reading(conn, 1, _hour(3), 4.0)
    latest = client.get("/api/stations").json()["data"][0]["latest"]
    assert latest["level_msl"] == 4.69
    assert latest["ts"] == _iso(_hour(1))


def test_ingested_at_is_newest_risk_row_with_offset(api):
    client, conn = api
    assert client.get("/api/stations").json()["meta"]["ingested_at"] is None
    _station(conn, 1, stem_order=1, role="main")
    t = _hour(0, 10)
    _risk(conn, 1, "normal", t)
    meta = client.get("/api/stations").json()["meta"]
    assert meta["ingested_at"] == _iso(t)
    assert meta["ingested_at"].endswith("+07:00")


def test_list_covers_every_reporting_station_not_just_the_corridor(api):
    client, conn = api
    _station(conn, 1, stem_order=1, role="main")
    _station(conn, 10)                     # off the corridor, reported an hour ago
    _station(conn, 11)                     # off the corridor, a risk row but no reading kept
    _station(conn, 12)                     # last reported 5 days ago: off the map
    _station(conn, 13)                     # never reported
    _reading(conn, 10, _hour(1), 2.0, -1.5)
    _reading(conn, 12, _hour(120), 2.0, -1.5)
    _risk(conn, 11, "watch", datetime.now(timezone.utc))
    data = client.get("/api/stations").json()["data"]
    assert [s["id"] for s in data] == [1, 10, 11]          # corridor (stem_order) first, then by id
    assert data[1]["stem_order"] is None and data[1]["role"] is None


def test_list_marks_a_gauge_that_duplicates_another(api):
    client, conn = api
    _station(conn, 1, stem_order=1, role="main")
    _station(conn, 2, lat=14.0001)                 # another agency's gauge 10 m away
    _reading(conn, 2, _hour(1), 2.0, -1.5)
    conn.execute("UPDATE stations SET same_place_as = 1 WHERE id = 2")
    data = {s["id"]: s for s in client.get("/api/stations").json()["data"]}
    assert data[1]["same_place_as"] is None and data[2]["same_place_as"] == 1


def test_empty_database_lists_nothing(api):
    client, _ = api
    r = client.get("/api/stations")
    assert r.status_code == 200 and r.json()["data"] == [] and r.json()["meta"]["ingested_at"] is None


# --- GET /api/stations/nearest -------------------------------------------------------------------

def test_nearest_orders_by_distance_over_all_stations(api):
    client, conn = api
    _station(conn, 1, lat=14.0, lon=100.0)                     # 0 km, not on the corridor
    _station(conn, 2, lat=14.0, lon=100.1, stem_order=2, role="main")   # ~10.8 km
    _station(conn, 3, lat=14.1, lon=100.0)                     # ~11.1 km
    _station(conn, 4, lat=14.2, lon=100.0)                     # ~22.2 km, beyond k=3
    _station(conn, 5, lat=None, lon=None)                      # no coordinates: never returned
    r = client.get("/api/stations/nearest", params={"lat": 14.0, "lon": 100.0, "k": 3})
    assert r.status_code == 200
    data = r.json()["data"]
    assert [s["id"] for s in data] == [1, 2, 3]
    assert [s["distance_km"] for s in data] == [0.0, 10.8, 11.1]
    assert set(data[0]) == STATION_SUMMARY_KEYS | {"distance_km"}


def test_nearest_defaults_to_three_and_respects_k(api):
    client, conn = api
    for i in range(1, 6):
        _station(conn, i, lat=14.0 + i * 0.01, lon=100.0)
    assert len(client.get("/api/stations/nearest", params={"lat": 14.0, "lon": 100.0}).json()["data"]) == 3
    assert len(client.get("/api/stations/nearest", params={"lat": 14.0, "lon": 100.0, "k": 10}).json()["data"]) == 5


def test_nearest_bad_input_is_400(api):
    client, _ = api
    for params in ({}, {"lat": 14.0}, {"lon": 100.0}, {"lat": "x", "lon": 100.0},
                   {"lat": 14.0, "lon": 100.0, "k": 0}, {"lat": 14.0, "lon": 100.0, "k": 11},
                   {"lat": 14.0, "lon": 100.0, "k": "many"}, {"lat": 95, "lon": 100.0}):
        r = client.get("/api/stations/nearest", params=params)
        assert r.status_code == 400, params
        assert r.json()["error"]["code"] == "bad_request"


# --- GET /api/stations/{id} ----------------------------------------------------------------------

def test_detail_has_banks_and_both_directions_of_edges(api):
    client, conn = api
    _station(conn, 1, lat=14.1, lon=100.1, stem_order=1, role="main", oldcode="C.7A",
             left_bank=5.5, right_bank=5.6, min_bank=3.0, ground_level=4.2, critical_level_msl=5.4)
    _station(conn, 2, oldcode="C.35", stem_order=2, role="main")
    _station(conn, 3, oldcode="C.99", stem_order=3, role="main")
    conn.execute("INSERT INTO station_edges (upstream_id, downstream_id, approx_travel_hours) VALUES (1, 2, 6.5)")
    conn.execute("INSERT INTO station_edges (upstream_id, downstream_id, approx_travel_hours) VALUES (2, 3, NULL)")
    _reading(conn, 2, _hour(0), 4.69, 0.34, 1254.0, 5)
    _risk(conn, 2, "warning", datetime.now(timezone.utc), hours=3.5)

    r = client.get("/api/stations/2")
    assert r.status_code == 200
    d = r.json()["data"]
    assert set(d) == DETAIL_KEYS
    assert d["oldcode"] == "C.35" and d["latest"]["diff_wl_bank"] == 0.34 and d["risk"]["hours_to_bank"] == 3.5
    assert [u["station"]["oldcode"] for u in d["upstream"]] == ["C.7A"]
    assert d["upstream"][0]["approx_travel_hours"] == 6.5
    assert set(d["upstream"][0]["station"]) == STATION_SUMMARY_KEYS
    assert [x["station"]["oldcode"] for x in d["downstream"]] == ["C.99"]
    assert d["downstream"][0]["approx_travel_hours"] is None

    up = client.get("/api/stations/1").json()["data"]
    assert (up["left_bank"], up["right_bank"], up["min_bank"], up["ground_level"], up["critical_level_msl"]) == \
        (5.5, 5.6, 3.0, 4.2, 5.4)
    assert up["upstream"] == [] and [x["station"]["id"] for x in up["downstream"]] == [2]


def test_detail_unknown_or_unlinked_station(api):
    client, conn = api
    _station(conn, 7)
    d = client.get("/api/stations/7").json()["data"]
    assert d["upstream"] == [] and d["downstream"] == [] and d["left_bank"] is None and d["risk"] is None
    r = client.get("/api/stations/999999")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


# --- GET /api/stations/{id}/readings -------------------------------------------------------------

def test_readings_are_thinned_to_one_per_hour_oldest_first(api):
    client, conn = api
    _station(conn, 1)
    # Three readings in one hour, one reading two hours back, and a gap at 3 hours ago.
    _reading(conn, 1, _hour(5, 0), 4.00, 0.10, 800.0)
    _reading(conn, 1, _hour(5, 20), 4.05, 0.15, 810.0)
    _reading(conn, 1, _hour(5, 40), 4.10, 0.20, 820.0)
    _reading(conn, 1, _hour(2, 0), 4.50, 0.30, None)
    _reading(conn, 1, _hour(1, 0), 4.60, None, 1000.0)

    r = client.get("/api/stations/1/readings", params={"hours": 72})
    assert r.status_code == 200
    body = r.json()["data"]
    assert body["station_id"] == 1
    assert [x["ts"] for x in body["readings"]] == [_iso(_hour(5)), _iso(_hour(2)), _iso(_hour(1))]
    assert set(body["readings"][0]) == {"ts", "level_msl", "diff_wl_bank", "discharge"}
    assert body["readings"][0]["level_msl"] == 4.00          # first reading of the hour
    assert body["readings"][1]["discharge"] is None


def test_readings_window_follows_hours(api):
    client, conn = api
    _station(conn, 1)
    _reading(conn, 1, _hour(2), 4.0)
    _reading(conn, 1, _hour(30), 3.0)
    _reading(conn, 1, _hour(100), 2.0)
    default = client.get("/api/stations/1/readings").json()["data"]["readings"]
    assert len(default) == 2                                   # 72 h by default
    short = client.get("/api/stations/1/readings", params={"hours": 24}).json()["data"]["readings"]
    assert [x["level_msl"] for x in short] == [4.0]
    wide = client.get("/api/stations/1/readings", params={"hours": 168}).json()["data"]["readings"]
    assert len(wide) == 3


def test_readings_bad_hours_and_unknown_station(api):
    client, conn = api
    _station(conn, 1)
    for hours in (0, 169, "abc"):
        r = client.get("/api/stations/1/readings", params={"hours": hours})
        assert r.status_code == 400 and r.json()["error"]["code"] == "bad_request", hours
    r = client.get("/api/stations/424242/readings")
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"


def test_readings_empty_for_station_without_data(api):
    client, conn = api
    _station(conn, 1)
    assert client.get("/api/stations/1/readings").json()["data"] == {"station_id": 1, "readings": []}


# --- GET /api/shelters ---------------------------------------------------------------------------

def _shelters(conn):
    rows = [
        ("near-patients", 14.0, 100.0, True, False),     # 0 km
        ("far-pets", 14.2, 100.0, False, True),          # ~22.2 km
        ("mid-both", 14.1, 100.0, True, True),           # ~11.1 km
    ]
    for name, lat, lon, pat, pet in rows:
        conn.execute("INSERT INTO shelters (name, lat, lon, capacity, accepts_patients, accepts_pets, phone) "
                     "VALUES (%s, %s, %s, 100, %s, %s, '02-000-0000')", (name, lat, lon, pat, pet))


def _names(body):
    return [s["name"] for s in body["data"]]


def test_shelters_sorted_by_distance_with_rounded_km(api):
    client, conn = api
    _shelters(conn)
    r = client.get("/api/shelters", params={"lat": 14.0, "lon": 100.0})
    assert r.status_code == 200
    assert _names(r.json()) == ["near-patients", "mid-both", "far-pets"]
    assert [s["distance_km"] for s in r.json()["data"]] == [0.0, 11.1, 22.2]
    assert set(r.json()["data"][0]) == SHELTER_KEYS


def test_shelters_without_point_sorted_by_id_and_distance_null(api):
    client, conn = api
    _shelters(conn)
    body = client.get("/api/shelters").json()
    assert _names(body) == ["near-patients", "far-pets", "mid-both"]
    assert all(s["distance_km"] is None for s in body["data"])


def test_shelters_filter_by_patients_and_pets(api):
    client, conn = api
    _shelters(conn)
    assert _names(client.get("/api/shelters", params={"patients": "true"}).json()) == ["near-patients", "mid-both"]
    assert _names(client.get("/api/shelters", params={"pets": "true"}).json()) == ["far-pets", "mid-both"]
    both = client.get("/api/shelters", params={"patients": "true", "pets": "true", "lat": 14.0, "lon": 100.0})
    assert _names(both.json()) == ["mid-both"]
    assert both.json()["data"][0]["distance_km"] == 11.1
    # false means no filter
    assert len(client.get("/api/shelters", params={"patients": "false"}).json()["data"]) == 3


def test_shelters_half_a_point_is_400(api):
    client, conn = api
    _shelters(conn)
    assert client.get("/api/shelters", params={"lat": 14.0}).status_code == 400
    assert client.get("/api/shelters", params={"lon": 100.0}).status_code == 400


# --- common/seed.py: stem_order and role from data/corridor_stations.csv --------------------------

def test_seed_loads_stem_order_and_role_from_csv(api):
    client, conn = api
    _station(conn, 2609)                 # C.35 on the main stem, position 10
    _station(conn, 2611)                 # C.36 branch: no stem position
    _station(conn, 2795)                 # C.2 main, position 1
    _station(conn, 999001)               # not in the CSV: stays null
    seed.main()
    got = {row[0]: (row[1], row[2]) for row in conn.execute("SELECT id, stem_order, role FROM stations").fetchall()}
    assert got[2609] == (10, "main")
    assert got[2611] == (None, "branch")
    assert got[2795] == (1, "main")
    assert got[999001] == (None, None)
    # The seeded corridor shows up in the list endpoint.
    assert [s["id"] for s in client.get("/api/stations").json()["data"]] == [2795, 2609, 2611]


def test_shelters_limit(api):
    client, conn = api
    _shelters(conn)
    assert _names(client.get("/api/shelters", params={"lat": 14.0, "lon": 100.0, "limit": 2}).json()) == \
        ["near-patients", "mid-both"]
    assert client.get("/api/shelters", params={"limit": 0}).status_code == 400
