"""GISTDA satellite flood extent for the web app's flood tab (docs/api.md → Flood).

Cells come from ingest/gistda_flood.py. Every number here is a count or a sum of GISTDA's cells; nothing is
estimated. Area is in km², and in rai (×625) because that is how Thai flood reports state it.
"""
import h3
import psycopg
from fastapi import APIRouter, Depends, Query

from api.common import ApiError, envelope, get_db

router = APIRouter()

PERIODS = ("3days", "7days")
RAI_PER_KM2 = 625
# Area level → its (name, code) columns. Grouping uses the name, else the code, so a layer with only codes still splits.
LEVELS = {"province": [("province", "pv_code")],
          "amphoe": [("province", "pv_code"), ("amphoe", "ap_code")],
          "tambon": [("province", "pv_code"), ("amphoe", "ap_code"), ("tambon", "tb_code")]}
CELL_RES = (5, 6, 7, 8, 9)       # 9 = the cells as GISTDA sent them; lower = their parents, summed
# Pad the box by about a parent cell so parents on the edge of the screen are not cut in half.
PAD_DEG = {5: 0.2, 6: 0.08, 7: 0.03, 8: 0.012, 9: 0.0}


def _period(period: str) -> str:
    if period not in PERIODS:
        raise ApiError(400, "bad_request", f"period must be one of {', '.join(PERIODS)}")
    return period


def _layer(conn: psycopg.Connection, period: str) -> dict:
    """When the stored layer was downloaded, and the range of dates GISTDA gives its cells. Nulls before the first."""
    row = conn.execute("SELECT fetched_at, observed_from, observed_to FROM flood_fetches WHERE period = %s "
                       "ORDER BY fetched_at DESC LIMIT 1", (period,)).fetchone()
    return {"period": period, "fetched_at": row and row["fetched_at"],
            "observed_from": row and row["observed_from"], "observed_to": row and row["observed_to"]}


def _area(km2: float | None) -> dict:
    km2 = km2 or 0.0
    return {"area_km2": round(km2, 4), "area_rai": round(km2 * RAI_PER_KM2)}


def _filters(province: str | None, amphoe: str | None) -> tuple[str, list]:
    sql, params = "", []
    if province:
        sql += " AND (province = %s OR pv_code = %s)"
        params += [province, province]
    if amphoe:
        sql += " AND (amphoe = %s OR ap_code = %s)"
        params += [amphoe, amphoe]
    return sql, params


@router.get("/api/flood/areas")
def flood_areas(period: str = "3days", level: str = "province",
                province: str | None = Query(None, max_length=100), amphoe: str | None = Query(None, max_length=100),
                limit: int = Query(100, ge=1, le=1000), conn: psycopg.Connection = Depends(get_db)):
    """Flooded area per province / amphoe / tambon, largest first, plus the total over the same filter."""
    _period(period)
    if level not in LEVELS:
        raise ApiError(400, "bad_request", f"level must be one of {', '.join(LEVELS)}")
    where, params = _filters(province, amphoe)
    cols = LEVELS[level]
    group = ", ".join(f"coalesce({name}, {code})" for name, code in cols)
    pick = ", ".join(f"max({name}) AS {name}" for name, _ in cols)
    code = cols[-1][1]
    rows = conn.execute(
        f"SELECT {pick}, max({code}) AS code, count(*) AS cells, sum(area_km2) AS km2, "
        "sum(lat * area_km2) / nullif(sum(area_km2), 0) AS lat, sum(lon * area_km2) / nullif(sum(area_km2), 0) AS lon "
        f"FROM flood_cells WHERE period = %s{where} GROUP BY {group} ORDER BY km2 DESC, {group} LIMIT %s",
        [period, *params, limit]).fetchall()
    total = conn.execute(f"SELECT count(*) AS cells, sum(area_km2) AS km2 FROM flood_cells WHERE period = %s{where}",
                         [period, *params]).fetchone()
    areas = [{"province": r["province"], "amphoe": r.get("amphoe"), "tambon": r.get("tambon"), "code": r["code"],
              "cells": r["cells"], **_area(r["km2"]),
              "lat": None if r["lat"] is None else round(r["lat"], 5),
              "lon": None if r["lon"] is None else round(r["lon"], 5)} for r in rows]
    return envelope(conn, {**_layer(conn, period), "level": level,
                           "total": {"cells": total["cells"], **_area(total["km2"])}, "areas": areas})


def _bbox(bbox: str) -> tuple[float, float, float, float]:
    try:
        w, s, e, n = (float(x) for x in bbox.split(","))
    except ValueError:
        raise ApiError(400, "bad_request", "bbox must be minLon,minLat,maxLon,maxLat")
    if not (-180 <= w < e <= 180 and -90 <= s < n <= 90):
        raise ApiError(400, "bad_request", "bbox must be minLon,minLat,maxLon,maxLat with min < max")
    return w, s, e, n


@router.get("/api/flood/cells")
def flood_cells(bbox: str = Query(..., max_length=100), period: str = "3days", res: int = Query(9),
                limit: int = Query(5000, ge=1, le=20000), conn: psycopg.Connection = Depends(get_db)):
    """Flooded H3 cells in the box (the map's view), largest first. res 9 is GISTDA's own cells; 5–8 sums them into
    parent cells for a zoomed-out map. share = flooded part of the cell (0–1). truncated: more than `limit`."""
    _period(period)
    if res not in CELL_RES:
        raise ApiError(400, "bad_request", f"res must be one of {', '.join(map(str, CELL_RES))}")
    w, s, e, n = _bbox(bbox)
    pad = PAD_DEG[res]
    box = [period, s - pad, n + pad, w - pad, e + pad]
    where = "period = %s AND lat BETWEEN %s AND %s AND lon BETWEEN %s AND %s"
    if res == 9:
        rows = conn.execute(f"SELECT h3, 1 AS cells, area_km2 AS km2, province, amphoe, tambon, observed_at "
                            f"FROM flood_cells WHERE {where} ORDER BY area_km2 DESC, h3 LIMIT %s",
                            [*box, limit + 1]).fetchall()
    else:
        rows = conn.execute(f"SELECT h3_r{res} AS h3, count(*) AS cells, sum(area_km2) AS km2 FROM flood_cells "
                            f"WHERE {where} GROUP BY 1 ORDER BY km2 DESC, 1 LIMIT %s", [*box, limit + 1]).fetchall()
    cells = []
    for r in rows[:limit]:
        c = {"h3": r["h3"], "cells": r["cells"], "area_km2": round(r["km2"], 4),
             "share": round(min(r["km2"] / h3.cell_area(r["h3"], "km^2"), 1.0), 3)}
        if res == 9:
            c |= {"province": r["province"], "amphoe": r["amphoe"], "tambon": r["tambon"],
                  "observed_at": r["observed_at"]}
        cells.append(c)
    return envelope(conn, {**_layer(conn, period), "res": res, "truncated": len(rows) > limit, "cells": cells})
