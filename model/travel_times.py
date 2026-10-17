"""Estimate approx_travel_hours for data/station_edges.csv from history (CLAUDE.md §13 day 1).

For each edge, take hourly levels for both stations, turn them into 6 h rises, and pick the lag
(0–72 h) where the upstream rise best correlates with the downstream rise. Writes the CSV in place.

    python -m model.travel_times --year 2025
"""
import argparse
import csv
from datetime import date, timedelta

from common.config import ROOT
from ingest import thaiwater

EDGES_CSV = ROOT / "data" / "station_edges.csv"
MAX_LAG_H = 72
RISE_H = 6
MIN_OVERLAP = 500


def hourly(rows: list[dict]) -> dict:
    """{hour_start: level}, using the last reading inside each hour."""
    out = {}
    for r in rows:
        out[r["ts"].replace(minute=0, second=0)] = r["level_msl"]
    return out


def rises(h: dict) -> dict:
    return {t: v - h[t - timedelta(hours=RISE_H)] for t, v in h.items() if t - timedelta(hours=RISE_H) in h}


def corr(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < MIN_OVERLAP:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sx = sum((x - mx) ** 2 for x in xs) ** 0.5
    sy = sum((y - my) ** 2 for y in ys) ** 0.5
    if not sx or not sy:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy)


def best_lag(up: dict, down: dict) -> tuple[int | None, float | None]:
    best = (None, None)
    for lag in range(0, MAX_LAG_H + 1):
        d = timedelta(hours=lag)
        keys = [t for t in up if t + d in down]
        c = corr([up[t] for t in keys], [down[t + d] for t in keys])
        if c is not None and (best[1] is None or c > best[1]):
            best = (lag, c)
    return best


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--year", type=int, default=2025)
    args = p.parse_args()
    start, end = date(args.year, 1, 1), min(date(args.year, 12, 31), date.today())
    with open(EDGES_CSV, encoding="utf-8") as f:
        edges = list(csv.DictReader(f))
    cache = {}

    def series(sid: int) -> dict:
        if sid not in cache:
            cache[sid] = rises(hourly(thaiwater.fetch_waterlevel_history(sid, start, end)))
        return cache[sid]

    for e in edges:
        up, down = series(int(e["upstream_id"])), series(int(e["downstream_id"]))
        lag, c = best_lag(up, down)
        print(f"{e['note']}: lag={lag} h corr={c and round(c, 2)} overlap_up={len(up)} down={len(down)}", flush=True)
        # Keep weak fits out: a lag from a correlation under 0.5 is noise, not travel time.
        e["approx_travel_hours"] = lag if lag is not None and c is not None and c >= 0.5 else ""
    with open(EDGES_CSV, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(edges[0].keys()))
        w.writeheader()
        w.writerows(edges)


if __name__ == "__main__":
    main()
