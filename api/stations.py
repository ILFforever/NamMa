"""Stations and shelters (docs/api.md → Stations, Shelters). Shapes mirror src/api/types.ts in namma-web.

Numbers come from the database only; distances are computed here with agent.geo.haversine_km.
Route order matters: /nearest is registered before /{station_id}.
"""
from fastapi import APIRouter, Depends, Query
import psycopg

from agent.geo import haversine_km
from api.common import ApiError, envelope, get_db

router = APIRouter()

SUMMARY_KEYS = ("id", "oldcode", "name_th", "name_en", "lat", "lon", "river", "province", "amphoe",
                "agency", "is_key_station", "stem_order", "role", "same_place_as")

# One row per station: newest reading (LATERAL, not N+1) and its risk row. Bank levels are for detail only.
_STATION_SQL = """
SELECT s.id, s.oldcode, s.name_th, s.name_en, s.lat, s.lon, s.river, s.province, s.amphoe, s.agency,
       s.is_key_station, s.stem_order, s.role, s.basin, s.river_key, s.river_order, s.same_place_as,
       s.left_bank, s.right_bank, s.min_bank, s.ground_level, s.critical_level_msl,
       l.ts AS latest_ts, l.level_msl AS latest_level_msl, l.diff_wl_bank AS latest_diff_wl_bank,
       l.situation_level AS latest_situation_level, l.discharge AS latest_discharge,
       r.risk_tier, r.trend_cm_per_hr, r.hours_to_bank, r.computed_at AS risk_computed_at
FROM stations s
LEFT JOIN LATERAL (
    SELECT w.ts, w.level_msl, w.diff_wl_bank, w.situation_level, w.discharge
    FROM waterlevel_readings w
    WHERE w.station_id = s.id
    ORDER BY w.ts DESC
    LIMIT 1
) l ON true
LEFT JOIN station_risk r ON r.station_id = s.id
"""

# The whole network the map shows: every station that has reported in the last 3 days or has a risk row (~800),
# plus the demo corridor's CSV stations even when they are quiet. Long-dead stations stay off the map, and so do:
# - ThaiWater's second listing of RID gauges under a source prefix ("ridhydro_C.36"): a twin of the plain-code
#   station, or a gate/weir/canal gauge whose bank level makes it read "over the bank" (agent.tools treats the
#   twins the same way). Their own pages and the chat still reach them by id.
# - Coordinates outside Thailand (one RID row has its longitude in the latitude's range), which would zoom the
#   map out to half the world.
_LISTED = """(l.ts > now() - interval '3 days' OR r.station_id IS NOT NULL
    OR s.stem_order IS NOT NULL OR s.role IS NOT NULL)
    AND COALESCE(s.oldcode, '') !~ '^[a-z]+_'
    AND s.lat BETWEEN 5 AND 21 AND s.lon BETWEEN 97 AND 106"""


def _station_rows(conn: psycopg.Connection, where: str, params=()) -> list[dict]:
    return conn.execute(f"{_STATION_SQL} WHERE {where} ORDER BY s.stem_order NULLS LAST, s.id", params).fetchall()


def _summary(r: dict) -> dict:
    latest = None
    if r["latest_ts"] is not None:
        latest = {"ts": r["latest_ts"], "level_msl": r["latest_level_msl"],
                  "diff_wl_bank": r["latest_diff_wl_bank"], "situation_level": r["latest_situation_level"],
                  "discharge": r["latest_discharge"]}
    risk = None
    if r["risk_tier"] is not None:
        risk = {"tier": r["risk_tier"], "trend_cm_per_hr": r["trend_cm_per_hr"],
                "hours_to_bank": r["hours_to_bank"], "computed_at": r["risk_computed_at"]}
    return {**{k: r[k] for k in SUMMARY_KEYS}, "latest": latest, "risk": risk}


@router.get("/api/stations")
def list_stations(conn: psycopg.Connection = Depends(get_db)):
    rows = _station_rows(conn, _LISTED)
    return envelope(conn, [_summary(r) for r in rows])


@router.get("/api/stations/nearest")
def nearest_stations(lat: float | None = Query(None, ge=-90, le=90),
                     lon: float | None = Query(None, ge=-180, le=180),
                     k: int = Query(3, ge=1, le=10),
                     conn: psycopg.Connection = Depends(get_db)):
    if lat is None or lon is None:
        raise ApiError(400, "bad_request", "lat and lon are required")
    coords = conn.execute("SELECT id, lat, lon FROM stations WHERE lat IS NOT NULL AND lon IS NOT NULL").fetchall()
    ranked = sorted((haversine_km(lat, lon, c["lat"], c["lon"]), c["id"]) for c in coords)[:k]
    by_id = {}
    if ranked:
        by_id = {r["id"]: r for r in _station_rows(conn, "s.id = ANY(%s::integer[])", ([i for _, i in ranked],))}
    data = [{**_summary(by_id[sid]), "distance_km": round(dist, 1)} for dist, sid in ranked]
    return envelope(conn, data)


@router.get("/api/stations/{station_id}")
def station_detail(station_id: int, conn: psycopg.Connection = Depends(get_db)):
    rows = _station_rows(conn, "s.id = %s", (station_id,))
    if not rows:
        raise ApiError(404, "not_found", f"station {station_id} not found")
    r = rows[0]
    upstream = conn.execute(
        "SELECT upstream_id AS other_id, approx_travel_hours FROM station_edges "
        "WHERE downstream_id = %s ORDER BY upstream_id", (station_id,)).fetchall()
    downstream = conn.execute(
        "SELECT downstream_id AS other_id, approx_travel_hours FROM station_edges "
        "WHERE upstream_id = %s ORDER BY downstream_id", (station_id,)).fetchall()
    linked_ids = [e["other_id"] for e in upstream + downstream]
    linked = {}
    if linked_ids:
        linked = {x["id"]: _summary(x) for x in _station_rows(conn, "s.id = ANY(%s::integer[])", (linked_ids,))}

    def links(edges: list[dict]) -> list[dict]:
        return [{"station": linked[e["other_id"]], "approx_travel_hours": e["approx_travel_hours"]} for e in edges]

    data = {**_summary(r),
            "left_bank": r["left_bank"], "right_bank": r["right_bank"], "min_bank": r["min_bank"],
            "ground_level": r["ground_level"], "critical_level_msl": r["critical_level_msl"],
            "upstream": links(upstream), "downstream": links(downstream)}
    return envelope(conn, data)


@router.get("/api/stations/{station_id}/readings")
def station_readings(station_id: int, hours: int = Query(72, ge=1, le=168),
                     conn: psycopg.Connection = Depends(get_db)):
    if conn.execute("SELECT 1 FROM stations WHERE id = %s", (station_id,)).fetchone() is None:
        raise ApiError(404, "not_found", f"station {station_id} not found")
    # One reading per Bangkok hour (the first in that hour), oldest first. Missing hours stay missing.
    rows = conn.execute(
        "SELECT ts, level_msl, diff_wl_bank, discharge FROM ("
        "  SELECT DISTINCT ON (date_trunc('hour', ts)) ts, level_msl, diff_wl_bank, discharge"
        "  FROM waterlevel_readings"
        "  WHERE station_id = %s AND ts > now() - make_interval(hours => %s::int)"
        "  ORDER BY date_trunc('hour', ts), ts"
        ") h ORDER BY ts", (station_id, hours)).fetchall()
    readings = [{"ts": r["ts"], "level_msl": r["level_msl"], "diff_wl_bank": r["diff_wl_bank"],
                 "discharge": r["discharge"]} for r in rows]
    return envelope(conn, {"station_id": station_id, "readings": readings})


@router.get("/api/shelters")
def list_shelters(lat: float | None = Query(None, ge=-90, le=90),
                  lon: float | None = Query(None, ge=-180, le=180),
                  patients: bool = False,
                  pets: bool = False,
                  limit: int = Query(50, ge=1, le=500),
                  conn: psycopg.Connection = Depends(get_db)):
    """~12,000 DDPM shelters: always capped by limit. accepts_patients is true only when DDPM confirms
    bedridden patients are accepted; accepts_bedridden keeps DDPM's null (= not stated)."""
    if (lat is None) != (lon is None):
        raise ApiError(400, "bad_request", "lat and lon must be given together")
    rows = conn.execute(
        "SELECT id, name, lat, lon, capacity, accepts_patients, accepts_pets, phone, is_open, current_residents, "
        "accepts_bedridden, province, district, source, source_updated_at AS updated_at FROM shelters "
        "WHERE (NOT %s OR accepts_patients) AND (NOT %s OR accepts_pets) ORDER BY id",
        (patients, pets)).fetchall()
    if lat is None:
        data = [{**r, "distance_km": None} for r in rows[:limit]]
    else:
        ranked = sorted(((haversine_km(lat, lon, r["lat"], r["lon"]), r["id"], r) for r in rows),
                        key=lambda t: (t[0], t[1]))
        data = [{**r, "distance_km": round(d, 1)} for d, _, r in ranked[:limit]]
    return envelope(conn, data)
