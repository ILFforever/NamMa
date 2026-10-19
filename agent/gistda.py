"""GISTDA for the chat: satellite flood extent near a point, and flood recurrence 2011–2023.

- Extent comes from the 3-day and 7-day layers ingest/gistda_flood.py stores (the old flood-extent-1day point query
  is dead), so a chat reply makes no GISTDA call for it.
- Recurrence is a live point query (needs GISTDA_API_KEY in the API-Key header). Its response shape is from another
  project's recorded responses (Oct 2026), not yet one of ours, so every raw response is archived under
  var/raw/gistda to check the parser against.

Both return None when there is nothing to say (no layer stored yet, no key, any error): GISTDA is extra context,
never a blocker for an answer.
"""
import json
import math
import os
import time
from datetime import datetime

import h3
import httpx
from psycopg.rows import dict_row

from agent.geo import haversine_km
from common.config import RAW_DIR
from ingest.thaiwater import BKK

NEAR_KM = 5.0
DAYS = {"3days": 3, "7days": 7}

RECURRENCE_URL = "https://api-gateway.gistda.or.th/api/2.0/resources/gi-service/v1.0/disasters/flood-recurrence"
TIMEOUT_S = 4          # this runs inside a chat reply
CACHE_S = 3600
BACKOFF_S = 600        # after a failure, skip calls for 10 min: a dead endpoint costs one timeout, not one per chat
_cache: dict[tuple, tuple[float, dict | None]] = {}
_failed_at = 0.0


def check_flood_extent(conn, lat: float, lon: float) -> dict | None:
    """From the newest stored layer with cells in it, 3-day first: is the point's own cell flooded, how far is the
    nearest flooded cell, and how much is flooded within NEAR_KM.

    {period, days, at_point, nearest_km (None: none within NEAR_KM), area_km2_nearby, radius_km, as_of, source}
    as_of is the newest date GISTDA gives the cells, else when we fetched the layer. None before the first fetch.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        fetches = {p: cur.execute("SELECT fetched_at, cells, observed_to FROM flood_fetches WHERE period = %s "
                                  "ORDER BY fetched_at DESC LIMIT 1", (p,)).fetchone() for p in DAYS}
        period = next((p for p in DAYS if fetches[p] and fetches[p]["cells"]), None) or \
            next((p for p in DAYS if fetches[p]), None)
        if period is None:
            return None
        dlat = NEAR_KM / 111.0
        dlon = NEAR_KM / (111.0 * max(math.cos(math.radians(lat)), 0.1))
        rows = cur.execute("SELECT h3, lat, lon, area_km2 FROM flood_cells WHERE period = %s "
                           "AND lat BETWEEN %s AND %s AND lon BETWEEN %s AND %s",
                           (period, lat - dlat, lat + dlat, lon - dlon, lon + dlon)).fetchall()
    near = [(haversine_km(lat, lon, r["lat"], r["lon"]), r) for r in rows]
    near = [(d, r) for d, r in near if d <= NEAR_KM]
    here = h3.latlng_to_cell(lat, lon, h3.get_resolution(near[0][1]["h3"])) if near else None
    nearest = min((d for d, _ in near), default=None)
    f = fetches[period]
    return {"period": period, "days": DAYS[period],
            "at_point": any(r["h3"] == here for _, r in near),
            "nearest_km": None if nearest is None else round(nearest, 1),
            "area_km2_nearby": round(sum(r["area_km2"] for _, r in near), 4),
            "radius_km": NEAR_KM,
            "as_of": (f["observed_to"] or f["fetched_at"]).astimezone(BKK).isoformat(),
            "source": "GISTDA"}


def parse_recurrence(data) -> dict | None:
    """[{subdistrict_name, district_name, province_name, total, detail: [{year, freq}]}] → {tambon, amphoe, province,
    years}: the years with any flooding. None for {"result": "not found"} or anything else."""
    rows = data.get("data") if isinstance(data, dict) else data
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
        return None
    r = rows[0]
    years = set()
    for d in r.get("detail") or []:
        try:
            if float(d.get("freq") or 0) > 0:
                years.add(int(d["year"]))
        except (TypeError, ValueError, KeyError, AttributeError):
            continue
    if not years:
        return None
    return {"tambon": r.get("subdistrict_name"), "amphoe": r.get("district_name"),
            "province": r.get("province_name"), "years": sorted(years), "source": "GISTDA (2011–2023)"}


def get_flood_recurrence(lat: float, lon: float) -> dict | None:
    global _failed_at
    key = os.environ.get("GISTDA_API_KEY")
    if not key or time.time() - _failed_at < BACKOFF_S:
        return None
    ck = (round(lat, 3), round(lon, 3))
    hit = _cache.get(ck)
    if hit and time.time() - hit[0] < CACHE_S:
        return hit[1]
    try:
        r = httpx.get(RECURRENCE_URL, params={"lat": lat, "lon": lon}, timeout=TIMEOUT_S,
                      headers={"API-Key": key, "User-Agent": "NamMa flood assistant (hackathon)"})
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        _failed_at = time.time()
        print(f"gistda recurrence failed: {e!r}".replace(key, "***"), flush=True)
        return None
    path = RAW_DIR / "gistda" / f"recurrence_{datetime.now():%Y%m%d_%H%M%S}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False).replace(key, "***"), encoding="utf-8")
    out = parse_recurrence(data)
    _cache[ck] = (time.time(), out)
    return out
