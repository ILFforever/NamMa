"""Shelters from DDPM (ปภ.) → the shelters table. The only module that talks to shelter.disaster.go.th.

    python -m ingest.shelters        # import now (the scheduler does it once a day)

Source: the public map layer behind https://shelter.disaster.go.th (ArcGIS FeatureServer, ~20,000 shelters,
live open/occupancy status). No licence is stated, so we fetch politely (once a day, 1 request/s) and credit ปภ.
The open dataset on catalog.disaster.go.th (dpm-gd002) is older (2024) and has no Bangkok, Bang Ban, open
status or bedridden field, so it is not used.

Only public, non-deleted shelters are kept. Coordinator names, emails and phones are personal data: never
requested, never stored.
"""
import time
from datetime import datetime, timedelta, timezone

import httpx

LAYER = "https://shelter.disaster.go.th/api/v1/arcgis/shelter-layer/FeatureServer/0/query"
PAGE = 2000                     # the layer's maxRecordCount
MIN_INTERVAL_S = 1.0
REFRESH = timedelta(hours=24)
FIELDS = ("shelter_code,name,capacity,current_residents,is_active,is_public_visible,deleted_at,lat,lng,"
          "province_name,district_name,qualification_accept_bedridden,qualification_accept_elderly,"
          "qualification_accept_disabled,updated_at")


def fetch_page(offset: int) -> dict:
    r = httpx.get(LAYER, timeout=120, headers={"User-Agent": "NamMa flood assistant (hackathon)"}, params={
        "where": "is_public_visible=1", "outFields": FIELDS, "returnGeometry": "false", "orderByFields": "OBJECTID",
        "resultOffset": offset, "resultRecordCount": PAGE, "f": "json"})
    r.raise_for_status()
    body = r.json()
    if "error" in body:                       # ArcGIS reports errors with HTTP 200
        raise RuntimeError(f"DDPM shelter layer: {body['error']}")
    return body


def fetch_all() -> list[dict]:
    out, offset = [], 0
    while True:
        body = fetch_page(offset)
        rows = [f["attributes"] for f in body.get("features", [])]
        out += rows
        if not body.get("exceededTransferLimit") and len(rows) < PAGE:
            return out
        offset += PAGE
        time.sleep(MIN_INTERVAL_S)


def _flag(v) -> bool | None:
    """DDPM flags are 1 / 0 / null. null stays None: unknown is not 'no'."""
    return None if v is None else bool(v)


def _ts(ms) -> datetime | None:
    return datetime.fromtimestamp(ms / 1000, timezone.utc) if ms else None


def parse(rows: list[dict]) -> list[dict]:
    """Raw layer attributes → shelters rows. Pure; tested against tests/fixtures/ddpm_shelters.json."""
    out = []
    for r in rows:
        lat, lon, code = r.get("lat"), r.get("lng"), (r.get("shelter_code") or "").strip()
        if r.get("deleted_at") or not r.get("is_public_visible") or not code or not r.get("name"):
            continue
        if lat is None or lon is None or not (5 < lat < 21 and 97 < lon < 106):   # inside Thailand
            continue
        out.append({
            "source_code": code, "name": r["name"].strip(), "lat": lat, "lon": lon,
            "capacity": r.get("capacity") or None, "current_residents": r.get("current_residents"),
            "is_open": _flag(r.get("is_active")),
            "accepts_bedridden": _flag(r.get("qualification_accept_bedridden")),
            "accepts_elderly": _flag(r.get("qualification_accept_elderly")),
            "accepts_disabled": _flag(r.get("qualification_accept_disabled")),
            "province": r.get("province_name"), "district": r.get("district_name"),
            "source_updated_at": _ts(r.get("updated_at")),
        })
    return out


def store(conn, shelters: list[dict]) -> int:
    """Upsert by DDPM code; DDPM shelters missing from this import (closed down, hidden) are removed."""
    now = datetime.now(timezone.utc)
    for s in shelters:
        conn.execute(
            "INSERT INTO shelters (source, source_code, name, lat, lon, capacity, current_residents, is_open, "
            "accepts_bedridden, accepts_elderly, accepts_disabled, accepts_patients, accepts_pets, province, district, "
            "source_updated_at, imported_at) VALUES ('ddpm', %(source_code)s, %(name)s, %(lat)s, %(lon)s, %(capacity)s, "
            "%(current_residents)s, %(is_open)s, %(accepts_bedridden)s, %(accepts_elderly)s, %(accepts_disabled)s, "
            "%(accepts_bedridden)s IS TRUE, false, %(province)s, %(district)s, %(source_updated_at)s, %(now)s) "
            "ON CONFLICT (source, source_code) WHERE source_code IS NOT NULL DO UPDATE SET name = EXCLUDED.name, "
            "lat = EXCLUDED.lat, lon = EXCLUDED.lon, capacity = EXCLUDED.capacity, "
            "current_residents = EXCLUDED.current_residents, is_open = EXCLUDED.is_open, "
            "accepts_bedridden = EXCLUDED.accepts_bedridden, accepts_elderly = EXCLUDED.accepts_elderly, "
            "accepts_disabled = EXCLUDED.accepts_disabled, accepts_patients = EXCLUDED.accepts_patients, "
            "province = EXCLUDED.province, district = EXCLUDED.district, "
            "source_updated_at = EXCLUDED.source_updated_at, imported_at = EXCLUDED.imported_at",
            {**s, "now": now})
    if shelters:     # an empty import is a fetch problem, not 20,000 shelters closing: keep what we have
        conn.execute("DELETE FROM shelters WHERE source = 'ddpm' AND imported_at < %s", (now,))
    return len(shelters)


def due(conn) -> bool:
    last = conn.execute("SELECT max(imported_at) FROM shelters WHERE source = 'ddpm'").fetchone()[0]
    return last is None or datetime.now(timezone.utc) - last > REFRESH


def run_once(conn) -> int:
    n = store(conn, parse(fetch_all()))
    conn.commit()
    return n


def main() -> None:
    from common.db import connect
    with connect() as conn:
        print(f"imported {run_once(conn)} DDPM shelters")


if __name__ == "__main__":
    main()
