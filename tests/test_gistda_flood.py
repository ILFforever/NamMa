"""GISTDA flood layers (ingest/gistda_flood.py), the flood API (api/flood.py) and the chat's lookup (agent/gistda.py).

GISTDA's property names are undocumented, so the features here are built to the shape other projects report
(OGC API Features, one H3 res-9 cell per feature, pv/ap/tb names and codes): they test the parser's fallbacks,
not GISTDA. Paging goes through httpx.MockTransport; nothing calls GISTDA. The DB tests skip without Postgres.
"""
import gzip
import json
from datetime import datetime, timedelta, timezone

import h3
import httpx
import pytest

from agent import chat, gistda, risk, tools
from ingest import gistda_flood as gf

BANG_BAN = (14.41, 100.46)
KEY = "k-secret-123"


def _feature(cell: str, *, with_id: bool = True, **props) -> dict:
    ring = [[lon, lat] for lat, lon in h3.cell_to_boundary(cell)]
    return {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [ring + ring[:1]]},
            "properties": ({"h3": cell} if with_id else {}) | props}


def _layer(n_cells: int = 7, at=BANG_BAN) -> list[dict]:
    """n_cells (≤ 7) flooded cells: the one at `at` first, then its neighbours. Every other one is half flooded."""
    centre = h3.latlng_to_cell(*at, 9)
    cells = ([centre] + sorted(c for c in h3.grid_disk(centre, 1) if c != centre))[:n_cells]
    return [_feature(c, pv_tn="จังหวัดพระนครศรีอยุธยา", ap_tn="อำเภอบางบาล", tb_tn="ตำบลบางหลวงโดด",
                     tb_idn="140504", h3_area=round(h3.cell_area(c, "m^2") * (0.5 if i % 2 else 1.0), 1),
                     date="2026-10-18", building=4 * i, population=str(10 * i), pop_affected=12 * i, link="https://example.org/?api_key=" + KEY)
            for i, c in enumerate(cells)]


# --- parsing ---------------------------------------------------------------------------------------

def test_parse_feature_reads_reported_names_and_keeps_the_rest():
    cell = h3.latlng_to_cell(*BANG_BAN, 9)
    c = gf.parse_feature(_layer(1)[0])
    assert c["h3"] == cell and (c["lat"], c["lon"]) == pytest.approx(h3.cell_to_latlng(cell))
    assert (c["province"], c["amphoe"], c["tambon"]) == ("พระนครศรีอยุธยา", "บางบาล", "บางหลวงโดด")
    assert (c["pv_code"], c["ap_code"], c["tb_code"]) == ("14", "1405", "140504")      # derived from tb_idn
    assert c["observed_at"] == datetime(2026, 10, 18, tzinfo=gf.BKK)
    assert c["area_unit"] is None and c["raw_area"] > 100_000                          # h3_area: unit decided later
    assert c["props"] == {"pop_affected": 0}                                           # URL dropped: may carry the key
    assert (c["buildings"], c["population"], c["schools"]) == (0, 0, None)              # counts read, absent = None


def test_parse_feature_finds_the_cell_without_a_named_key():
    cell = h3.latlng_to_cell(*BANG_BAN, 9)
    assert gf.parse_feature(_feature(cell, with_id=False, grid=h3.str_to_int(cell)))["h3"] == cell
    assert gf.parse_feature(_feature(cell, with_id=False, flood_rai=12))["h3"] == cell   # from the geometry
    assert gf.parse_feature({"type": "Feature", "geometry": None, "properties": {"x": 1}}) is None


def test_parse_feature_dates_and_explicit_units():
    cell = h3.latlng_to_cell(*BANG_BAN, 9)
    c = gf.parse_feature(_feature(cell, area_rai=20, acq_date="2569-10-17 06:30:00"))
    assert (c["raw_area"], c["area_unit"]) == (20, "rai")
    assert c["observed_at"] == datetime(2026, 10, 17, 6, 30, tzinfo=gf.BKK)          # พ.ศ. → ค.ศ., Thai time
    assert gf._when(1_760_000_000_000) == datetime.fromtimestamp(1_760_000_000, timezone.utc)
    assert gf._when("not a date") is None


def test_parse_feature_reads_gistdas_real_shape():
    """Properties as GISTDA sent them on 2026-10-19 (flood/3days, first feature; ids shortened)."""
    cell = "8964a493227ffff"
    real = {"_area": 1259.5259331933048, "_createdAt": "2026-10-19T10:58:21.799Z", "_id": "6ad5f7cd",
            "ap_en": "AMPHOE MUEANG CHACHOENGSAO", "ap_idn": 2401, "ap_tn": "อ.เมืองฉะเชิงเทรา", "building": 0,
            "f_area": 1259.5259331933048, "file_name": "cm2_20261017_1710, cm2_20261018_0607, cm4_20261019_0627",
            "h3_address": cell, "h3_area": 124552.40640633086, "hospital": 0, "length_road": 0, "mongo_id": "6ad5f3b6",
            "population": 169.55, "pv_en": "Changwat Chachoengsao", "pv_idn": 24, "pv_tn": "จ.ฉะเชิงเทรา",
            "re_royin": "East", "rice_area": 9.18, "school": 0, "tb_en": "BANG KAHAI", "tb_idn": 240114,
            "tb_tn": "ต.บางกระไห"}
    c = gf.parse_feature(_feature(cell, with_id=False, **real))
    assert c["h3"] == cell and (c["raw_area"], c["area_unit"]) == (1259.5259331933048, "m2")   # f_area, not h3_area
    assert (c["province"], c["amphoe"], c["tambon"]) == ("ฉะเชิงเทรา", "เมืองฉะเชิงเทรา", "บางกระไห")
    assert (c["pv_code"], c["ap_code"], c["tb_code"]) == ("24", "2401", "240114")
    assert c["observed_at"] == datetime(2026, 10, 19, 6, 27, tzinfo=gf.BKK)                    # newest scene
    assert (c["buildings"], c["population"], c["schools"], c["hospitals"]) == (0, 169.55, 0, 0)
    [row], unit = gf.finish([c])
    assert row["area_km2"] == pytest.approx(0.00126, abs=1e-5) and unit == "cell"              # ~1% of the cell


@pytest.mark.parametrize("scale, unit", [(1.0, "km2"), (625.0, "rai"), (1e6, "m2"), (1e9, "cell")])
def test_area_unit_is_judged_over_the_whole_layer(scale, unit):
    cells = sorted(h3.grid_disk(h3.latlng_to_cell(*BANG_BAN, 9), 1))
    parsed = [{"h3": c, "raw_area": h3.cell_area(c, "km^2") * scale * (1 if i else 0.3), "area_unit": None}
              for i, c in enumerate(cells)]
    assert gf.area_unit(parsed) == unit
    assert gf.area_unit([{**p, "raw_area": None} for p in parsed]) == "cell"


def test_finish_dedupes_converts_and_caps_at_the_cell():
    cell = h3.latlng_to_cell(*BANG_BAN, 9)
    full = h3.cell_area(cell, "km^2")
    a = gf.parse_feature(_feature(cell, area_m2=1000))
    b = gf.parse_feature(_feature(cell, area_m2=full * 1e6 * 3))        # same cell again, impossible area
    rows, _ = gf.finish([a, b])
    assert len(rows) == 1 and rows[0]["area_km2"] == pytest.approx(full)
    assert rows[0]["h3_r5"] == h3.cell_to_parent(cell, 5) and rows[0]["h3_r8"] == h3.cell_to_parent(cell, 8)
    rows, unit = gf.finish([gf.parse_feature(f) for f in _layer(7)])
    assert unit == "m2" and sorted(round(r["area_km2"] / h3.cell_area(r["h3"], "km^2"), 2) for r in rows) == \
        [0.5, 0.5, 0.5, 1.0, 1.0, 1.0, 1.0]



# --- fetching --------------------------------------------------------------------------------------

def _gistda(pages: dict[int, list[dict]], total: int | None = None, links: bool = False, status: int = 200):
    """A fake GISTDA: pages by offset. Records each request."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if status != 200:
            return httpx.Response(status, json={"code": "INVALID_API_KEY", "status": status})
        off = int(request.url.params["offset"])
        feats = pages.get(off, [])
        body = {"type": "FeatureCollection", "features": feats, "numberReturned": len(feats),
                "timeStamp": datetime.now(timezone.utc).isoformat(),
                "links": [{"rel": "self", "href": str(request.url)}]}
        if total is not None:
            body["numberMatched"] = total
        if links and off + len(feats) < sum(len(p) for p in pages.values()):
            body["links"].append({"rel": "next", "href": "…"})     # links=False: only a self link, as some servers do
        return httpx.Response(200, json=body)

    client = httpx.Client(base_url=gf.BASE, transport=httpx.MockTransport(handler), headers={"API-Key": KEY})
    return client, seen


@pytest.fixture()
def small_pages(monkeypatch):
    monkeypatch.setattr(gf, "PAGE", 3)
    monkeypatch.setattr(gf, "MIN_INTERVAL_S", 0)
    feats = _layer(7)
    return {0: feats[:3], 3: feats[3:6], 6: feats[6:]}


@pytest.mark.parametrize("total, links", [(7, False), (None, True), (None, False)])
def test_fetch_layer_pages_until_the_end(small_pages, total, links):
    client, seen = _gistda(small_pages, total=total, links=links)
    first = gf.fetch_page(client, "3days", 0)
    got = [f for page in gf.fetch_layer(client, "3days", first) for f in page["features"]]
    assert len(got) == 7 and [int(r.url.params["offset"]) for r in seen] == [0, 3, 6]
    assert all(r.headers["API-Key"] == KEY and KEY not in str(r.url) for r in seen)    # key in the header only


def test_fetch_layer_stops_when_offset_is_ignored(small_pages):
    client, _ = _gistda({0: small_pages[0], 3: small_pages[0], 6: small_pages[0]}, total=9)
    with pytest.raises(gf.GistdaError, match="offset"):
        list(gf.fetch_layer(client, "3days", gf.fetch_page(client, "3days", 0)))


def test_fetch_layer_refuses_a_layer_that_ends_early(small_pages):
    client, _ = _gistda({0: small_pages[0], 3: small_pages[3]}, total=7)
    with pytest.raises(gf.GistdaError, match="ended at 6 of its 7"):
        list(gf.fetch_layer(client, "3days", gf.fetch_page(client, "3days", 0)))


def test_a_refused_key_is_a_clear_error():
    client, _ = _gistda({}, status=401)
    with pytest.raises(gf.GistdaError, match="refused the key"):
        gf.fetch_page(client, "3days", 0)


def test_fingerprint_ignores_timestamp_and_links(small_pages):
    client, _ = _gistda(small_pages, total=7)
    a, b = gf.fetch_page(client, "3days", 0), gf.fetch_page(client, "3days", 0)
    assert a["timeStamp"] != b["timeStamp"] or a["links"] == b["links"]
    assert gf.fingerprint(a) == gf.fingerprint(b)
    assert gf.fingerprint(a) != gf.fingerprint({**a, "numberMatched": 8})


# --- storing, API, chat (Postgres) -------------------------------------------------------------------

def _snapshots(tmp_path, period="3days"):
    return sorted((tmp_path / "gistda" / "flood" / period).iterdir())


def _store_layer(conn, monkeypatch, tmp_path, pages, period="3days", total=None):
    monkeypatch.setattr(gf, "RAW_DIR", tmp_path)
    client, seen = _gistda(pages, total=total if total is not None else sum(len(p) for p in pages.values()))
    return gf.run_once(conn, period, KEY, client=client), seen


def test_run_once_stores_the_layer_and_skips_an_unchanged_one(api, monkeypatch, tmp_path, small_pages):
    _, conn = api
    summary, seen = _store_layer(conn, monkeypatch, tmp_path, small_pages)
    assert summary["cells"] == 7 and len(seen) == 3
    assert summary["observed_to"] == datetime(2026, 10, 18, tzinfo=gf.BKK)
    assert conn.execute("SELECT count(*), count(DISTINCT h3_r7) FROM flood_cells WHERE period = '3days'").fetchone()[0] == 7
    unit, by_prov = conn.execute("SELECT area_unit, by_province FROM flood_fetches").fetchone()
    assert unit == "m2" and list(by_prov) == ["พระนครศรีอยุธยา"]
    [snap] = _snapshots(tmp_path)
    assert snap.suffix == "" and sorted(p.name for p in snap.iterdir()) == [
        "0000000.json.gz", "0000003.json.gz", "0000006.json.gz"]
    for page in snap.iterdir():
        text = gzip.open(page, "rt", encoding="utf-8").read()
        assert KEY not in text and "links" not in json.loads(text)
    today = datetime.now(gf.BKK).date()
    assert summary["day"] == today
    assert conn.execute("SELECT day, count(*) FROM flood_cells_history GROUP BY day").fetchall() == [(today, 7)]

    again, seen = _store_layer(conn, monkeypatch, tmp_path, small_pages)
    assert again is None and len(seen) == 1                        # only the first page was asked for
    assert conn.execute("SELECT count(*) FROM flood_fetches").fetchone()[0] == 1
    assert len(_snapshots(tmp_path)) == 1


def test_a_failed_download_keeps_the_stored_layer(api, monkeypatch, tmp_path, small_pages):
    _, conn = api
    _store_layer(conn, monkeypatch, tmp_path, small_pages)
    changed = {0: _layer(3, at=(14.6, 100.5)), 3: []}                                  # ends at 3 of 99
    with pytest.raises(gf.GistdaError):
        _store_layer(conn, monkeypatch, tmp_path, changed, total=99)
    assert conn.execute("SELECT count(*) FROM flood_cells").fetchone()[0] == 7
    assert conn.execute("SELECT count(*) FROM flood_cells_history").fetchone()[0] == 7
    assert [d.suffix for d in _snapshots(tmp_path)] == [""]                           # no partial snapshot left


def test_history_keeps_one_copy_per_day(api, monkeypatch, tmp_path, small_pages):
    _, conn = api
    _store_layer(conn, monkeypatch, tmp_path, small_pages)
    conn.execute("UPDATE flood_cells_history SET day = day - 1")                       # make it yesterday's copy
    _store_layer(conn, monkeypatch, tmp_path, {0: _layer(3, at=(14.6, 100.5))})
    today = datetime.now(gf.BKK).date()
    by_day = "SELECT day, count(*) FROM flood_cells_history GROUP BY day ORDER BY day"
    assert conn.execute(by_day).fetchall() == [(today - timedelta(days=1), 7), (today, 3)]
    _store_layer(conn, monkeypatch, tmp_path, small_pages)                             # a later layer the same day
    assert conn.execute(by_day).fetchall() == [(today - timedelta(days=1), 7), (today, 7)]
    assert len(_snapshots(tmp_path)) == 3                                              # every stored layer's pages


def test_old_and_partial_snapshots_are_dropped(api, monkeypatch, tmp_path, small_pages):
    _, conn = api
    root = tmp_path / "gistda" / "flood" / "3days"
    (root / "20200101_000000_000000").mkdir(parents=True)                             # past GISTDA_SNAPSHOT_KEEP_DAYS
    (root / "20991231_000000_000000.partial").mkdir()                                  # a run stopped part-way
    _store_layer(conn, monkeypatch, tmp_path, small_pages)
    [snap] = _snapshots(tmp_path)
    assert snap.name not in ("20200101_000000_000000", "20991231_000000_000000") and snap.suffix == ""


def test_supervise_runs_the_loop_in_a_low_priority_process(monkeypatch):
    started = []

    class Stop(Exception):
        pass

    class FakeProcess:
        def __init__(self, cmd, cwd):
            started.append(cmd)

        def wait(self):
            return 1                                                                   # the fetcher died

    def sleep(_seconds):
        raise Stop

    monkeypatch.setattr(gf.subprocess, "Popen", FakeProcess)
    monkeypatch.setattr(gf.time, "sleep", sleep)
    with pytest.raises(Stop):
        gf.supervise(10800)
    [cmd] = started
    assert cmd[:3] == ["nice", "-n", "10"] and cmd[4:] == ["-m", "ingest.gistda_flood", "--every", "10800"]


def test_flood_areas(api, monkeypatch, tmp_path, small_pages):
    client, conn = api
    empty = client.get("/api/flood/areas").json()["data"]
    assert empty["fetched_at"] is None and empty["total"] == {"cells": 0, "area_km2": 0.0, "area_rai": 0}
    assert empty["areas"] == []

    _store_layer(conn, monkeypatch, tmp_path, small_pages)
    conn.execute("UPDATE flood_cells SET amphoe = 'เสนา', ap_code = '1406', tambon = 'บ้านแพน', tb_code = '140601' "
                 "WHERE h3 IN (SELECT h3 FROM flood_cells ORDER BY h3 LIMIT 2)")
    d = client.get("/api/flood/areas", params={"period": "3days"}).json()["data"]
    assert set(d) == {"period", "fetched_at", "observed_from", "observed_to", "level", "total", "areas"}
    assert d["fetched_at"].endswith("+07:00") and d["observed_to"].startswith("2026-10-18")
    km2 = conn.execute("SELECT sum(area_km2) FROM flood_cells").fetchone()[0]
    assert d["total"] == {"cells": 7, "area_km2": round(km2, 4), "area_rai": round(km2 * 625)}
    [prov] = d["areas"]
    assert (prov["province"], prov["code"], prov["cells"]) == ("พระนครศรีอยุธยา", "14", 7)
    assert prov["lat"] == pytest.approx(BANG_BAN[0], abs=0.01) and prov["amphoe"] is None
    assert (prov["buildings"], prov["population"]) == (84, 210)                         # sums of GISTDA's counts

    amphoes = client.get("/api/flood/areas", params={"level": "amphoe", "province": "พระนครศรีอยุธยา"}).json()["data"]
    assert [(a["amphoe"], a["cells"]) for a in amphoes["areas"]] == [("บางบาล", 5), ("เสนา", 2)]
    tambons = client.get("/api/flood/areas", params={"level": "tambon", "province": "14", "amphoe": "1406"}).json()
    assert [(a["tambon"], a["code"]) for a in tambons["data"]["areas"]] == [("บ้านแพน", "140601")]
    assert tambons["data"]["total"]["cells"] == 2
    assert client.get("/api/flood/areas", params={"period": "7days"}).json()["data"]["areas"] == []
    for bad in ({"period": "1day"}, {"level": "village"}, {"limit": 0}):
        assert client.get("/api/flood/areas", params=bad).status_code == 400, bad


def test_flood_cells_in_view(api, monkeypatch, tmp_path, small_pages):
    client, conn = api
    _store_layer(conn, monkeypatch, tmp_path, small_pages)
    view = {"bbox": "100.40,14.35,100.52,14.47", "period": "3days"}
    d = client.get("/api/flood/cells", params=view).json()["data"]
    assert d["res"] == 9 and d["truncated"] is False and len(d["cells"]) == 7
    c = d["cells"][0]
    assert set(c) == {"h3", "cells", "area_km2", "share", "province", "amphoe", "tambon", "observed_at",
                      "buildings", "population", "schools", "hospitals"}
    assert c["schools"] is None and c["hospitals"] is None                             # not sent: unknown, not 0
    assert c["share"] == 1.0 and h3.is_valid_cell(c["h3"]) and c["amphoe"] == "บางบาล"   # largest first

    parent = client.get("/api/flood/cells", params={**view, "res": 7}).json()["data"]["cells"]
    assert sum(p["cells"] for p in parent) == 7 and all(h3.get_resolution(p["h3"]) == 7 for p in parent)
    assert sum(p["area_km2"] for p in parent) == pytest.approx(sum(x["area_km2"] for x in d["cells"]), abs=1e-3)
    assert set(parent[0]) == {"h3", "cells", "area_km2", "share"} and 0 < parent[0]["share"] < 1

    capped = client.get("/api/flood/cells", params={**view, "limit": 2}).json()["data"]
    assert capped["truncated"] is True and len(capped["cells"]) == 2
    away = client.get("/api/flood/cells", params={**view, "bbox": "99.0,13.0,99.1,13.1"}).json()["data"]
    assert away["cells"] == [] and away["truncated"] is False
    for bad in ({"bbox": "1,2,3"}, {"bbox": "100.5,14,100.4,15"}, {"bbox": "a,b,c,d"}, {"res": 4}, {"period": "x"}):
        assert client.get("/api/flood/cells", params={**view, **bad}).status_code == 400, bad
    assert client.get("/api/flood/cells").status_code == 400                           # bbox is required


def test_flood_near_a_place(api, monkeypatch, tmp_path, small_pages):
    client, conn = api
    before = client.get("/api/flood/near", params={"lat": BANG_BAN[0], "lon": BANG_BAN[1]}).json()["data"]
    assert before["fetched_at"] is None and before["nearest_km"] is None and before["nearby"]["cells"] == 0

    _store_layer(conn, monkeypatch, tmp_path, small_pages)
    here = client.get("/api/flood/near", params={"lat": BANG_BAN[0], "lon": BANG_BAN[1]}).json()["data"]
    km2 = conn.execute("SELECT sum(area_km2) FROM flood_cells").fetchone()[0]
    assert here["at_point"] is True and here["radius_km"] == 5.0
    assert here["nearby"] == {"cells": 7, "area_km2": round(km2, 4), "area_rai": round(km2 * 625)}
    away = client.get("/api/flood/near", params={"lat": BANG_BAN[0] + 0.02, "lon": BANG_BAN[1]}).json()["data"]
    assert away["at_point"] is False and 1.0 < away["nearest_km"] < 3.0
    week = client.get("/api/flood/near", params={"lat": BANG_BAN[0], "lon": BANG_BAN[1], "period": "7days"}).json()
    assert week["data"]["fetched_at"] is None and week["data"]["at_point"] is False   # only the asked-for period
    for bad in ({"lat": 91, "lon": 100}, {"lat": 14}, {"lat": 14, "lon": 100, "period": "1day"}):
        assert client.get("/api/flood/near", params=bad).status_code == 400, bad


def test_chat_flood_check_uses_the_stored_layer(api, monkeypatch, tmp_path, small_pages):
    _, conn = api
    assert gistda.check_flood_extent(conn, *BANG_BAN) is None                          # nothing fetched yet
    _store_layer(conn, monkeypatch, tmp_path, small_pages)
    here = gistda.check_flood_extent(conn, *BANG_BAN)
    assert here["at_point"] and here["days"] == 3 and here["nearest_km"] == pytest.approx(0, abs=0.2)
    assert here["as_of"] == "2026-10-18T00:00:00+07:00"
    near = gistda.check_flood_extent(conn, BANG_BAN[0] + 0.02, BANG_BAN[1])            # ~2 km north
    assert not near["at_point"] and 1.0 < near["nearest_km"] < 3.0
    assert near["area_km2_nearby"] == pytest.approx(here["area_km2_nearby"])
    far = gistda.check_flood_extent(conn, 13.0, 100.0)
    assert far["nearest_km"] is None and far["area_km2_nearby"] == 0

    # An empty 3-day layer falls back to the 7-day one.
    conn.execute("DELETE FROM flood_cells")
    conn.execute("UPDATE flood_fetches SET cells = 0")
    _store_layer(conn, monkeypatch, tmp_path, {0: _layer(2), 3: []}, period="7days")
    assert gistda.check_flood_extent(conn, *BANG_BAN)["days"] == 7


def test_chat_answer_shows_the_satellite_line(api, monkeypatch, tmp_path, small_pages):
    client, conn = api
    monkeypatch.setattr(tools, "_fill_history", lambda conn, ids: False)
    now = datetime.now().astimezone().replace(second=0, microsecond=0)
    conn.execute("INSERT INTO stations (id, oldcode, name_th, lat, lon, river, agency) "
                 "VALUES (9201, 'T.9', 'สถานีทดสอบ', 14.41, 100.46, 'แม่น้ำทดสอบ', 'RID')")
    for h in range(4):
        conn.execute("INSERT INTO waterlevel_readings (station_id, ts, level_msl, diff_wl_bank) VALUES (9201, %s, 4, -1)",
                     (now - timedelta(hours=3 - h),))
    risk.compute_all(conn, now)
    _store_layer(conn, monkeypatch, tmp_path, small_pages)

    r = client.post("/api/chat", json={"session_id": "flood-1", "location": {"lat": BANG_BAN[0], "lon": BANG_BAN[1]}})
    d = r.json()["data"]
    assert "ภาพดาวเทียม GISTDA 3 วันล่าสุด: พบน้ำท่วมที่จุดนี้" in d["reply"]
    [src] = [s for s in d["sources"] if s["type"] == "gistda"]
    assert src["kind"] == "flood_extent" and src["observed_at"] == "2026-10-18T00:00:00+07:00"
    assert src["summary"].startswith("ภาพดาวเทียม GISTDA 3 วันล่าสุด")
    # Named place: a district centre, so the area around it, not a yes/no for the point.
    r = chat.handle(conn, "U-flood", "line", text="บ้านอยู่บางบาล อยุธยา น้ำจะถึงไหม")
    assert "พื้นที่น้ำท่วมในรัศมี 5 กม. รวมราว" in r.text
