"""History backfill for the demo corridor (data/corridor_stations.csv).

    python -m ingest.backfill --coverage        # print % of hourly slots with data, per station-year; writes nothing
    python -m ingest.backfill --from 2018       # load history into waterlevel_readings
    python -m ingest.backfill --from 2018 --edges --workers 4   # also every station in data/station_edges.csv

Station-years already loaded are skipped, so an interrupted backfill can simply be rerun.

Run `python -m ingest.run` once first so the stations table is populated.
"""
import argparse
import csv
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

from common.config import ROOT
from common.db import connect
from ingest import store, thaiwater

CORRIDOR_CSV = ROOT / "data" / "corridor_stations.csv"
EDGES_CSV = ROOT / "data" / "station_edges.csv"
LOADED = 2000          # readings in a year above which a station-year counts as already fetched


def corridor_stations() -> list[dict]:
    with open(CORRIDOR_CSV, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def year_range(year: int) -> tuple[date, date]:
    today = date.today()
    return date(year, 1, 1), min(date(year, 12, 31), today)


def coverage(years: range) -> None:
    print("code     " + " ".join(f"{y:>5}" for y in years))
    for s in corridor_stations():
        cells = []
        for y in years:
            start, end = year_range(y)
            rows = thaiwater.parse_waterlevel_graph(int(s["id"]), thaiwater.fetch_waterlevel_graph(int(s["id"]), start, end))
            hours = {r["ts"].replace(minute=0) for r in rows}
            cells.append(f"{100 * len(hours) // ((end - start).days * 24 + 24):>4}%")
        print(f"{s['code']:<8} " + " ".join(cells), flush=True)


def edge_station_ids() -> set[int]:
    with open(EDGES_CSV, encoding="utf-8") as f:
        return {int(r[k]) for r in csv.DictReader(f) for k in ("upstream_id", "downstream_id")}


def _loaded(conn, year: int) -> set[int]:
    lo, hi = date(year, 1, 1), date(year + 1, 1, 1)
    return {r[0] for r in conn.execute("SELECT station_id FROM waterlevel_readings WHERE ts >= %s AND ts < %s "
                                       "GROUP BY 1 HAVING count(*) >= %s", (lo, hi, LOADED))}


def fetch_years(conn, ids, years, workers: int = 1) -> None:
    """History for each (station, year) not loaded yet. Requests run in threads; inserts stay on this connection."""
    jobs = [(sid, y) for y in years for sid in sorted(set(ids) - _loaded(conn, y))]
    print(f"{len(jobs)} station-years to fetch with {workers} workers", flush=True)
    with ThreadPoolExecutor(workers) as pool:
        futs = {pool.submit(thaiwater.fetch_waterlevel_history, sid, *year_range(y)): (sid, y) for sid, y in jobs}
        for n, f in enumerate(as_completed(futs), 1):
            sid, y = futs[f]
            try:
                rows = f.result()
            except Exception as e:                   # a dead station shouldn't stop the rest
                print(f"[{n}/{len(jobs)}] {sid} {y}: failed {e!r}", flush=True)
                continue
            store.insert_history(conn, rows)
            conn.commit()
            print(f"[{n}/{len(jobs)}] {sid} {y}: {len(rows)} readings", flush=True)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--from", dest="start", type=int, default=2018)
    p.add_argument("--to", dest="end", type=int, default=date.today().year)
    p.add_argument("--coverage", action="store_true")
    p.add_argument("--edges", action="store_true", help="also every station in data/station_edges.csv")
    p.add_argument("--workers", type=int, default=1)
    args = p.parse_args()
    years = range(args.start, args.end + 1)
    if args.coverage:
        coverage(years)
        return
    ids = {int(s["id"]) for s in corridor_stations()} | (edge_station_ids() if args.edges else set())
    with connect() as conn:
        fetch_years(conn, ids, years, args.workers)


if __name__ == "__main__":
    main()
