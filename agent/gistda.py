"""GISTDA for the chat: satellite flood extent near a point, and flood recurrence.

- Extent comes from the 3-day and 7-day layers ingest/gistda_flood.py stores (the old flood-extent-1day point query
  is dead), so a chat reply makes no GISTDA call for it.
- Recurrence is off until we hold our own copy of GISTDA's flood history. The old point query
  (gi-service/v1.0/disasters/flood-recurrence) is dead (404, checked from the VM 2026-10-19), and its replacement,
  features/flood-freq, takes 20 s to over 60 s for a map-box query, too slow for a chat reply. The copy plan
  (docs: "GISTDA flood history: own copy plan") fills flood_history_cells / flood_history_tambon; then
  get_flood_recurrence reads from those.

Both return None when there is nothing to say (no layer stored yet, no copy yet): GISTDA is extra context,
never a blocker for an answer.
"""
import math

import h3
from psycopg.rows import dict_row

from agent.geo import haversine_km
from ingest.thaiwater import BKK

NEAR_KM = 5.0
DAYS = {"3days": 3, "7days": 7}


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


def get_flood_recurrence(lat: float, lon: float) -> dict | None:
    """Off until flood_history_tambon exists (see the module docstring); then {tambon, amphoe, province, years, source}
    for the sub-district at the point, the shape answer.recurrence_text reads. Makes no network call."""
    return None
