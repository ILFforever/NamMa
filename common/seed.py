"""Load curated CSVs (station_edges, shelters) into the DB. Idempotent.

    python -m common.seed

Needs the stations table populated first (python -m ingest.run), because edges reference it.
"""
import csv

from common.config import ROOT
from common.db import connect

DATA = ROOT / "data"


def _rows(name: str) -> list[dict]:
    with open(DATA / name, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _num(v):
    return float(v) if v not in (None, "") else None


def main() -> None:
    with connect() as conn:
        known = {r[0] for r in conn.execute("SELECT id FROM stations")}
        edges = [e for e in _rows("station_edges.csv")
                 if int(e["upstream_id"]) in known and int(e["downstream_id"]) in known]
        for e in edges:
            conn.execute(
                "INSERT INTO station_edges (upstream_id, downstream_id, approx_travel_hours) VALUES (%s, %s, %s) "
                "ON CONFLICT (upstream_id, downstream_id) DO UPDATE SET approx_travel_hours = EXCLUDED.approx_travel_hours",
                (int(e["upstream_id"]), int(e["downstream_id"]), _num(e["approx_travel_hours"])))
        shelters = _rows("shelters.csv")
        for s in shelters:
            conn.execute(
                "INSERT INTO shelters (name, lat, lon, capacity, accepts_patients, accepts_pets, phone, notes) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (name) DO UPDATE SET lat = EXCLUDED.lat, "
                "lon = EXCLUDED.lon, capacity = EXCLUDED.capacity, accepts_patients = EXCLUDED.accepts_patients, "
                "accepts_pets = EXCLUDED.accepts_pets, phone = EXCLUDED.phone, notes = EXCLUDED.notes",
                (s["name"], float(s["lat"]), float(s["lon"]), int(s["capacity"] or 0) or None,
                 s["accepts_patients"] == "true", s["accepts_pets"] == "true", s["phone"] or None, s["notes"] or None))
        print(f"seeded {len(edges)} station_edges, {len(shelters)} shelters")


if __name__ == "__main__":
    main()
