"""GISTDA satellite flood extent for the web app's flood tab (docs/api.md → Flood).

Cells come from ingest/gistda_flood.py. Every number here is a count or a sum of GISTDA's cells; nothing is
estimated. Area is in km², and in rai (×625) because that is how Thai flood reports state it.
"""
import h3
import psycopg
from fastapi import APIRouter, Depends, Query

from agent import gistda
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
IMPACT = ("buildings", "population", "schools", "hospitals")   # GISTDA's counts in a cell; null = not sent


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


def _count(v: float | None) -> int | None:
    """A summed GISTDA count, whole; None when no cell carried it (unknown, not zero)."""
    return None if v is None else round(v)


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
        "sum(lat * area_km2) / nullif(sum(area_km2), 0) AS lat, sum(lon * area_km2) / nullif(sum(area_km2), 0) AS lon, "
        "sum(buildings) AS buildings, sum(population) AS population "
        f"FROM flood_cells WHERE period = %s{where} GROUP BY {group} ORDER BY km2 DESC, {group} LIMIT %s",
        [period, *params, limit]).fetchall()
    total = conn.execute(f"SELECT count(*) AS cells, sum(area_km2) AS km2 FROM flood_cells WHERE period = %s{where}",
                         [period, *params]).fetchone()
    areas = [{"province": r["province"], "amphoe": r.get("amphoe"), "tambon": r.get("tambon"), "code": r["code"],
              "cells": r["cells"], **_area(r["km2"]),
              "lat": None if r["lat"] is None else round(r["lat"], 5),
              "lon": None if r["lon"] is None else round(r["lon"], 5),
              "buildings": _count(r["buildings"]), "population": _count(r["population"])} for r in rows]
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
        rows = conn.execute(f"SELECT h3, 1 AS cells, area_km2 AS km2, province, amphoe, tambon, observed_at, "
                            f"{', '.join(IMPACT)} "
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
                  "observed_at": r["observed_at"], **{k: _count(r[k]) for k in IMPACT}}
        cells.append(c)
    return envelope(conn, {**_layer(conn, period), "res": res, "truncated": len(rows) > limit, "cells": cells})


@router.get("/api/flood/near")
def flood_near(lat: float = Query(..., ge=-90, le=90), lon: float = Query(..., ge=-180, le=180),
               period: str = "3days", conn: psycopg.Connection = Depends(get_db)):
    """Flooding around one point (a saved place): is its own cell flooded, how far is the nearest flooded cell, and
    the flooded area within radius_km. The same lookup the chat uses (agent/gistda.py), for one period."""
    _period(period)
    near = gistda.check_flood_extent(conn, lat, lon, period)
    return envelope(conn, {**_layer(conn, period),
                           "at_point": bool(near and near["at_point"]),
                           "nearest_km": near and near["nearest_km"],
                           "radius_km": gistda.NEAR_KM,
                           "nearby": {"cells": near["cells_nearby"] if near else 0,
                                      **_area(near["area_km2_nearby"] if near else 0)}})


# --- flood history 2011– (our copy of GISTDA's flood-freq, ingest/gistda_history.py) -----------------------------

def _history_years(conn: psycopg.Connection) -> list[int]:
    """Every year the copy has a flag for, FIRST_YEAR → the latest year any sub-district flooded."""
    from ingest.gistda_history import FIRST_YEAR
    mask = conn.execute("SELECT bit_or(years) AS m FROM flood_history_tambon").fetchone()["m"] or 0
    return list(range(FIRST_YEAR, FIRST_YEAR + mask.bit_length())) if mask else []


@router.get("/api/flood/history")
def flood_history(level: str = "province", province: str | None = Query(None, max_length=10),
                  conn: psycopg.Connection = Depends(get_db)):
    """Flooded years and flooded rai per year, per province (level=province) or per sub-district of one province
    (level=tambon&province=<pv code>). Codes are GISTDA's pv_idn / tb_idn, the same as the map's outlines.
    `provinces` lists the provinces copied so far: an area missing from `areas` in one of them never flooded
    2011–; in any other it is simply not copied yet."""
    from ingest.gistda_history import years_of
    if level not in ("province", "tambon"):
        raise ApiError(400, "bad_request", "level must be province or tambon")
    if level == "tambon" and not province:
        raise ApiError(400, "bad_request", "level=tambon needs province")
    copied = [r["pv_code"] for r in conn.execute("SELECT pv_code FROM flood_history_runs ORDER BY pv_code")]
    sql = "SELECT tb_code, pv_code, province, amphoe, tambon, years, rai_by_year FROM flood_history_tambon"
    rows = conn.execute(sql + (" WHERE pv_code = %s" if level == "tambon" else ""),
                        [province] if level == "tambon" else []).fetchall()
    if level == "tambon":
        areas = [{"code": r["tb_code"], "province": r["province"], "amphoe": r["amphoe"], "tambon": r["tambon"],
                  "years": years_of(r["years"]), "rai_by_year": r["rai_by_year"]} for r in rows]
    else:
        by_pv: dict[str, dict] = {}
        for r in rows:
            a = by_pv.setdefault(r["pv_code"], {"code": r["pv_code"], "province": r["province"], "amphoe": None,
                                                "tambon": None, "mask": 0, "rai_by_year": {}})
            a["mask"] |= r["years"]
            for y, rai in r["rai_by_year"].items():
                a["rai_by_year"][y] = a["rai_by_year"].get(y, 0.0) + rai
        areas = [{**{k: v for k, v in a.items() if k != "mask"}, "years": years_of(a["mask"]),
                  "rai_by_year": {y: round(v, 1) for y, v in sorted(a["rai_by_year"].items())}}
                 for a in by_pv.values()]
    areas.sort(key=lambda a: -max(a["rai_by_year"].values(), default=0))
    return envelope(conn, {"level": level, "province": province, "years": _history_years(conn),
                           "provinces": copied, "areas": areas, "source": "GISTDA"})
