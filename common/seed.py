"""Load curated CSVs (station_edges) into the DB. Idempotent. Shelters come from DDPM: python -m ingest.shelters.

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


def _corridor(conn) -> tuple[int, int]:
    """stem_order and role for the demo corridor (data/corridor_stations.csv). Returns (matched, listed)."""
    rows = _rows("corridor_stations.csv")
    matched = 0
    for r in rows:
        matched += conn.execute(
            "UPDATE stations SET stem_order = %s, role = %s WHERE id = %s",
            (int(r["stem_order"]) if r["stem_order"] else None, r["role"] or None, int(r["id"]))).rowcount
    return matched, len(rows)


def main() -> None:
    with connect() as conn:
        corridor_matched, corridor_listed = _corridor(conn)
        known = {r[0] for r in conn.execute("SELECT id FROM stations")}
        edges = [e for e in _rows("station_edges.csv")
                 if int(e["upstream_id"]) in known and int(e["downstream_id"]) in known]
        for e in edges:
            conn.execute(
                "INSERT INTO station_edges (upstream_id, downstream_id, approx_travel_hours) VALUES (%s, %s, %s) "
                "ON CONFLICT (upstream_id, downstream_id) DO UPDATE SET approx_travel_hours = EXCLUDED.approx_travel_hours",
                (int(e["upstream_id"]), int(e["downstream_id"]), _num(e["approx_travel_hours"])))
        from ingest import rivers
        on_rivers = rivers.run_once(conn)
        print(f"seeded corridor {corridor_matched}/{corridor_listed} stations, "
              f"{len(edges)} station_edges, {on_rivers} stations on rivers")


if __name__ == "__main__":
    main()
