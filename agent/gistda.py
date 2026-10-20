"""GISTDA for the chat: satellite flood extent near a point, and flood recurrence.

- Extent comes from the 3-day and 7-day layers ingest/gistda_flood.py stores (the old flood-extent-1day point query
  is dead), so a chat reply makes no GISTDA call for it. When neither shows water nearby, the 30-day layer says
  whether water was seen there earlier in the month (`earlier`): the water may have gone down.
- Recurrence comes from our own copy of GISTDA's flood history (flood_history_cells / flood_history_tambon, filled
  by ingest/gistda_history.py one province at a time), so a chat reply makes no GISTDA call for it either. The old
  point query (gi-service/v1.0/disasters/flood-recurrence) is dead, and features/flood-freq takes 20 s to over 60 s
  by map box, too slow for a chat reply.

Both return None when there is nothing to say (no layer stored yet, province not copied yet): GISTDA is extra
context, never a blocker for an answer.
"""
import math

import h3
from psycopg.rows import dict_row

from agent.geo import haversine_km
from ingest.gistda_history import RES as HISTORY_RES, years_of
from ingest.thaiwater import BKK

NEAR_KM = 5.0
DAYS = {"3days": 3, "7days": 7, "30days": 30}
CURRENT = ("3days", "7days")       # "is it flooded now"; 30days only answers "was it, this month"
TAMBON_KM = 2.0     # the sub-district is the one of the nearest historically flooded cell within this


def check_flood_extent(conn, lat: float, lon: float, period: str | None = None) -> dict | None:
    """From the newest stored layer with cells in it, 3-day first (or the given period's): is the point's own cell
    flooded, how far is the nearest flooded cell, and how much is flooded within NEAR_KM.

    {period, days, at_point, nearest_km (None: none within NEAR_KM), area_km2_nearby, cells_nearby, radius_km,
    as_of, earlier, source}. as_of is the newest date GISTDA gives the cells, else when we fetched the layer.
    earlier: with no water within NEAR_KM now, the 30-day layer's {days, nearest_km, area_km2_nearby, as_of} when
    it saw water there, else None. None before the first fetch.
    """
    periods = [period] if period else list(CURRENT)
    with conn.cursor(row_factory=dict_row) as cur:
        fetches = {p: _last_fetch(cur, p) for p in periods}
        period = next((p for p in periods if fetches[p] and fetches[p]["cells"]), None) or \
            next((p for p in periods if fetches[p]), None)
        if period is None:
            return None
        near = _nearby(cur, period, lat, lon)
        earlier = None
        if not near and period in CURRENT and (month := _last_fetch(cur, "30days")) and \
                (before := _nearby(cur, "30days", lat, lon)):
            earlier = {"days": DAYS["30days"], "nearest_km": round(min(d for d, _ in before), 1),
                       "area_km2_nearby": round(sum(r["area_km2"] for _, r in before), 4), "as_of": _as_of(month)}
    here = h3.latlng_to_cell(lat, lon, h3.get_resolution(near[0][1]["h3"])) if near else None
    nearest = min((d for d, _ in near), default=None)
    return {"period": period, "days": DAYS[period],
            "at_point": any(r["h3"] == here for _, r in near),
            "nearest_km": None if nearest is None else round(nearest, 1),
            "area_km2_nearby": round(sum(r["area_km2"] for _, r in near), 4),
            "cells_nearby": len(near),
            "radius_km": NEAR_KM,
            "as_of": _as_of(fetches[period]),
            "earlier": earlier,
            "source": "GISTDA"}


def _last_fetch(cur, period: str) -> dict | None:
    return cur.execute("SELECT fetched_at, cells, observed_to FROM flood_fetches WHERE period = %s "
                       "ORDER BY fetched_at DESC LIMIT 1", (period,)).fetchone()


def _as_of(fetch: dict) -> str:
    return (fetch["observed_to"] or fetch["fetched_at"]).astimezone(BKK).isoformat()


def _nearby(cur, period: str, lat: float, lon: float) -> list[tuple[float, dict]]:
    """(km, cell) for the period's flooded cells within NEAR_KM of the point."""
    dlat = NEAR_KM / 111.0
    dlon = NEAR_KM / (111.0 * max(math.cos(math.radians(lat)), 0.1))
    rows = cur.execute("SELECT h3, lat, lon, area_km2 FROM flood_cells WHERE period = %s "
                       "AND lat BETWEEN %s AND %s AND lon BETWEEN %s AND %s",
                       (period, lat - dlat, lat + dlat, lon - dlon, lon + dlon)).fetchall()
    near = [(haversine_km(lat, lon, r["lat"], r["lon"]), r) for r in rows]
    return [(d, r) for d, r in near if d <= NEAR_KM]


def get_flood_recurrence(conn, lat: float, lon: float) -> dict | None:
    """{tambon, amphoe, province, years, at_point, source}: the years GISTDA saw flooding in the sub-district of the
    nearest historically flooded cell within TAMBON_KM, and the years the point's own cell flooded ([] if never).
    None when no flooded cell lies that close: the province is not copied yet, or the area never flooded."""
    dlat = TAMBON_KM / 111.0
    dlon = TAMBON_KM / (111.0 * max(math.cos(math.radians(lat)), 0.1))
    with conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute("SELECT h3, lat, lon, years, tb_code FROM flood_history_cells "
                           "WHERE lat BETWEEN %s AND %s AND lon BETWEEN %s AND %s",
                           (lat - dlat, lat + dlat, lon - dlon, lon + dlon)).fetchall()
        near = sorted(((haversine_km(lat, lon, r["lat"], r["lon"]), r) for r in rows), key=lambda x: x[0])
        near = [r for d, r in near if d <= TAMBON_KM]
        if not near:
            return None
        here = h3.latlng_to_cell(lat, lon, HISTORY_RES)
        at_point = 0
        for r in near:
            at_point |= r["years"] if r["h3"] == here else 0
        tb = next((r["tb_code"] for r in near if r["tb_code"]), None)
        t = cur.execute("SELECT tambon, amphoe, province, years FROM flood_history_tambon WHERE tb_code = %s",
                        (tb,)).fetchone() if tb else None
    years = years_of(t["years"]) if t else years_of(near[0]["years"])
    return {"tambon": t and t["tambon"], "amphoe": t and t["amphoe"], "province": t and t["province"],
            "years": years, "at_point": years_of(at_point), "source": "GISTDA (2011–2024)"}
