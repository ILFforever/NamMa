"""Rivers for the web's river charts (docs/rivers-api.md, docs/api.md → Rivers). Shapes mirror src/api/types.ts.

Stations are grouped and ordered by ingest.rivers; a river here is its stations with a river_order (stand-ins for
the same place left out), upstream first, as StationSummary. Only rivers with at least two such stations.
"""
from fastapi import APIRouter, Depends, Query
import psycopg

from agent.geo import haversine_km
from agent.risk import TIERS
from api.common import ApiError, envelope, get_db
from api.stations import _station_rows, _summary

router = APIRouter()

MIN_STATIONS = 2
AT_RISK = {"watch", "warning", "danger"}
FALLBACK = 3          # rivers shown when nothing is at risk and no place is given: the best-gauged ones
PLACE_FIELDS = ("province", "amphoe", "name_th", "oldcode", "basin")   # what a place search matches besides the river


def _rivers(conn: psycopg.Connection, where: str = "TRUE", params=()) -> list[dict]:
    rows = _station_rows(conn, f"s.river_order IS NOT NULL AND {where}", params)
    by_key: dict[str, list[dict]] = {}
    for r in sorted(rows, key=lambda r: r["river_order"]):
        by_key.setdefault(r["river_key"], []).append(r)
    out = []
    for key, members in by_key.items():
        if len(members) < MIN_STATIONS:
            continue
        tiers = [r["risk_tier"] for r in members if r["risk_tier"] in TIERS]
        basins: list[str] = []
        for r in members:
            if r["basin"] and r["basin"] not in basins:
                basins.append(r["basin"])
        provinces: list[str] = []
        for r in members:
            if r["province"] and r["province"] not in provinces:
                provinces.append(r["province"])
        out.append({
            "key": key,
            "name_th": key.split("@")[0],
            "basins": basins,
            "provinces": provinces,             # upstream first; tells two same-named rivers apart
            "worst_tier": max(tiers, key=TIERS.index) if tiers else None,
            "_over": max((r["latest_diff_wl_bank"] for r in members if r["latest_diff_wl_bank"] is not None),
                         default=None),
            "_rows": members,
        })
    return out


def _public(river: dict, reason: str, distance_km: float | None = None) -> dict:
    return {k: v for k, v in river.items() if not k.startswith("_")} | {
        "reason": reason,
        "distance_km": None if distance_km is None else round(distance_km, 1),
        "stations": [_summary(r) for r in river["_rows"]],
    }


def _nearest_km(river: dict, lat: float, lon: float) -> float:
    return min(haversine_km(lat, lon, r["lat"], r["lon"]) for r in river["_rows"])


@router.get("/api/rivers")
def list_rivers(lat: float | None = Query(None, ge=-90, le=90),
                lon: float | None = Query(None, ge=-180, le=180),
                q: str | None = Query(None, max_length=60),
                limit: int = Query(6, ge=1, le=50),
                conn: psycopg.Connection = Depends(get_db)):
    """With q: rivers whose name contains q, then rivers with a station whose province, amphoe, name, code or basin
    contains q (each best-gauged first). Otherwise: rivers at watch or worse (worst first, then highest against
    its bank), then the river nearest lat/lon; if that is nothing, the best-gauged rivers."""
    if (lat is None) != (lon is None):
        raise ApiError(400, "bad_request", "lat and lon must be given together")
    rivers = _rivers(conn)
    near = (lambda r: _nearest_km(r, lat, lon)) if lat is not None else (lambda r: None)

    if q is not None and q.strip():
        needle = q.strip().casefold()
        by_size = lambda r: (-len(r["_rows"]), r["key"])  # noqa: E731
        named = sorted((r for r in rivers if needle in r["name_th"].casefold()), key=by_size)
        placed = sorted((r for r in rivers if r not in named and any(
            needle in str(s[f] or "").casefold() for s in r["_rows"] for f in PLACE_FIELDS)), key=by_size)
        return envelope(conn, [_public(r, "match", near(r)) for r in (named + placed)[:limit]])

    at_risk = sorted((r for r in rivers if r["worst_tier"] in AT_RISK),
                     key=lambda r: (-TIERS.index(r["worst_tier"]), -(r["_over"] if r["_over"] is not None else -99),
                                    r["key"]))[:limit]
    data = [_public(r, "at_risk", near(r)) for r in at_risk]
    if lat is not None and rivers:
        closest = min(rivers, key=lambda r: (_nearest_km(r, lat, lon), r["key"]))
        if all(d["key"] != closest["key"] for d in data):
            data.append(_public(closest, "near_you", near(closest)))
    if not data:
        best = sorted(rivers, key=lambda r: (-len(r["_rows"]), r["key"]))[:FALLBACK]
        data = [_public(r, "largest") for r in best]
    return envelope(conn, data)


@router.get("/api/rivers/{key}")
def river_detail(key: str, conn: psycopg.Connection = Depends(get_db)):
    found = _rivers(conn, "s.river_key = %s", (key,))
    if not found:
        raise ApiError(404, "not_found", f"river {key} not found")
    return envelope(conn, _public(found[0], "match"))
