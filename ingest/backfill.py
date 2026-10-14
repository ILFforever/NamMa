"""History backfill for the demo corridor (data/corridor_stations.csv).

    python -m ingest.backfill --coverage        # print % of hourly slots with data, per station-year; writes nothing
    python -m ingest.backfill --from 2018       # load history into waterlevel_readings

Run `python -m ingest.run` once first so the stations table is populated.
"""
import argparse
import csv
from datetime import date

from common.config import ROOT
from common.db import connect
from ingest import store, thaiwater

CORRIDOR_CSV = ROOT / "data" / "corridor_stations.csv"


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


def backfill(years: range) -> None:
    with connect() as conn:
        for s in corridor_stations():
            for y in years:
                start, end = year_range(y)
                rows = thaiwater.fetch_waterlevel_history(int(s["id"]), start, end)
                store.insert_history(conn, rows)
                conn.commit()
                print(f"{s['code']} {y}: {len(rows)} readings", flush=True)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--from", dest="start", type=int, default=2018)
    p.add_argument("--to", dest="end", type=int, default=date.today().year)
    p.add_argument("--coverage", action="store_true")
    args = p.parse_args()
    years = range(args.start, args.end + 1)
    coverage(years) if args.coverage else backfill(years)


if __name__ == "__main__":
    main()
