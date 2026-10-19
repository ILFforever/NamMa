"""ingest.rivers.group: grouping, same-place stand-ins and upstream-first order (no DB)."""
from datetime import datetime, timedelta, timezone

from ingest.rivers import Override, Station, group, load_overrides

NOW = datetime(2026, 10, 19, 23, 0, tzinfo=timezone(timedelta(hours=7)))
FRESH = NOW - timedelta(minutes=20)
STALE = NOW - timedelta(hours=6)


def st(sid, river="แม่น้ำเจ้าพระยา", lat=15.0, lon=100.0, bank=10.0, agency="RID", ts=FRESH):
    return Station(sid, river, lat, lon, bank, agency, ts)


def test_orders_upstream_first_by_bank_height():
    p = group([st(1, lat=14.37, bank=4.35), st(2, lat=15.67, bank=25.7), st(3, lat=15.43, bank=21.3, agency="HII")],
              now=NOW)
    assert sorted((v.river_order, k) for k, v in p.items()) == [(1, 2), (2, 3), (3, 1)]
    assert {v.river_key for v in p.values()} == {"แม่น้ำเจ้าพระยา"}


def test_same_place_keeps_rid_and_points_the_other_at_it():
    # C.35 and CPY011: 10 m apart, banks a metre apart (different surveys). One place, RID stands for it.
    p = group([st(83, lat=14.36913, lon=100.5769, bank=5.06, agency="HII"),
               st(2609, lat=14.3691, lon=100.5768, bank=4.35),
               st(9, lat=15.0, bank=13.2)], now=NOW)
    assert p[2609].river_order == 2 and p[2609].same_place_as is None
    assert p[83].river_order is None and p[83].same_place_as == 2609


def test_fresh_station_beats_a_stale_rid_one():
    p = group([st(1, lat=14.0, ts=STALE), st(2, lat=14.001, agency="HII")], now=NOW)
    assert p[2].same_place_as is None and p[1].same_place_as == 2


def test_far_apart_stations_with_one_name_are_separate_rivers():
    p = group([st(5, river="คลองใหญ่", lat=12.7, lon=101.3), st(6, river="คลองใหญ่", lat=12.75, lon=101.35),
               st(7, river="คลองใหญ่", lat=6.5, lon=101.3)], now=NOW)
    assert p[5].river_key == p[6].river_key == "คลองใหญ่"
    assert p[7].river_key == "คลองใหญ่@7"


def test_a_river_stays_whole_across_a_reservoir():
    # Ping: 113 km between the last Chiang Mai station and the first below Bhumibol.
    p = group([st(1, river="แม่น้ำปิง", lat=18.2, lon=98.9, bank=260), st(2, river="แม่น้ำปิง", lat=17.2, lon=99.0, bank=134)],
              now=NOW)
    assert p[1].river_key == p[2].river_key == "แม่น้ำปิง"


def test_stations_without_a_river_position_or_bank_are_left_out():
    p = group([st(1, river=None), st(2, river="  "), st(3, lat=None), st(4, bank=None), st(5, bank=0.0), st(6)],
              now=NOW)
    assert set(p) == {6}


def test_overrides_move_reorder_and_exclude():
    stations = [st(1, lat=15.0, bank=10), st(2, lat=14.6, bank=5), st(3, lat=14.2, bank=2),
                st(4, river="คลองบางบาล", lat=14.4)]
    p = group(stations, {3: Override(sort_bank=20), 2: Override(exclude=True),
                         4: Override(river_key="แม่น้ำเจ้าพระยา", sort_bank=7)}, now=NOW)
    assert 2 not in p
    assert sorted((v.river_order, k) for k, v in p.items()) == [(1, 3), (2, 1), (3, 4)]


def test_load_overrides_skips_comments_and_blank_fields(tmp_path):
    f = tmp_path / "o.csv"
    f.write_text("# a comment\nstation_id,river_key,sort_bank,exclude,note\n12,,3.5,,tidal\n13,แม่น้ำน้อย,,,\n14,,,1,\n",
                 encoding="utf-8")
    assert load_overrides(f) == {12: Override(sort_bank=3.5), 13: Override(river_key="แม่น้ำน้อย"),
                                 14: Override(exclude=True)}
    assert load_overrides(tmp_path / "missing.csv") == {}
