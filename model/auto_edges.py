"""Build station_edges for the whole country from live station metadata and one year of history.

    python -m model.auto_edges fetch              # load --year history for every candidate station (resumable)
    python -m model.auto_edges build              # score candidate pairs, write review CSV + append to station_edges.csv

Candidates: each station paired with its K nearest stations on the same river (MIN_KM to MAX_KM away).
A pair becomes an edge when
  - direction: the higher station (median level_msl) is upstream, by at least MIN_DROP_M (rivers run downhill);
  - connection: 6 h rises correlate (≥ MIN_CORR) at some lag 0–72 h, and the best lag over ±72 h does not
    point the other way. That lag is approx_travel_hours.
Edges implied by a chain of shorter ones (A→B→C makes A→C redundant) are dropped, so ancestors() stays local.
The hand-built corridor (data/corridor_stations.csv) is left alone: its edges were checked by hand.
"""
import argparse
import csv
import math
from datetime import datetime, timedelta, timezone

import numpy as np

from common.config import ROOT
from common.db import connect
from ingest.backfill import fetch_years

EDGES_CSV = ROOT / "data" / "station_edges.csv"
CORRIDOR_CSV = ROOT / "data" / "corridor_stations.csv"
REVIEW_CSV = ROOT / "model" / "artifacts" / "auto_edges.csv"
K = 2
MAX_KM = 150
MIN_KM = 1             # closer is a second gauge at the same site: datums differ, so height says nothing
MAX_LAG_H = 72
RISE_H = 6
MIN_OVERLAP = 500
MIN_CORR = 0.5
MIN_DROP_M = 0.2


def _km(a, b) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def _ids(path) -> set[int]:
    with open(path, encoding="utf-8") as f:
        return {int(r["id"]) for r in csv.DictReader(f)}


def stations(conn) -> dict[int, dict]:
    rows = conn.execute("SELECT id, oldcode, name_th, river, province, lat, lon FROM stations "
                        "WHERE river IS NOT NULL AND river <> '' AND lat IS NOT NULL AND lon IS NOT NULL").fetchall()
    return {r[0]: dict(zip(("id", "code", "name", "river", "province", "lat", "lon"), r)) for r in rows}


def candidates(st: dict[int, dict]) -> set[tuple[int, int]]:
    """Unordered (low id, high id) pairs: K nearest same-river neighbours, corridor stations excluded."""
    corridor = _ids(CORRIDOR_CSV)
    by_river: dict[str, list[dict]] = {}
    for s in st.values():
        if s["id"] not in corridor:
            by_river.setdefault(s["river"], []).append(s)
    pairs = set()
    for group in by_river.values():
        for s in group:
            near = sorted((_km((s["lat"], s["lon"]), (t["lat"], t["lon"])), t["id"]) for t in group if t is not s)
            near = [(d, t) for d, t in near if MIN_KM <= d <= MAX_KM]
            pairs |= {(min(s["id"], t), max(s["id"], t)) for d, t in near[:K]}
    return pairs


def hourly(conn, sid: int, year: int) -> np.ndarray:
    """Level per UTC hour of the year (last reading in the hour); NaN where missing."""
    t0 = datetime(year, 1, 1, tzinfo=timezone.utc)
    n = int((datetime(year + 1, 1, 1, tzinfo=timezone.utc) - t0).total_seconds() // 3600)
    out = np.full(n, np.nan)
    for ts, lv in conn.execute("SELECT ts, level_msl FROM waterlevel_readings WHERE station_id = %s "
                               "AND ts >= %s AND ts < %s ORDER BY ts", (sid, t0, t0 + timedelta(hours=n))):
        if lv is not None:
            out[int((ts - t0).total_seconds() // 3600)] = lv
    return out


def best_lag(a: np.ndarray, b: np.ndarray, lags) -> tuple[int | None, float | None]:
    """Lag L (hours) maximising corr(a[t], b[t + L]) over 6 h rises; positive L means a leads b."""
    ra, rb = a[RISE_H:] - a[:-RISE_H], b[RISE_H:] - b[:-RISE_H]
    best = (None, None)
    for L in lags:
        x, y = (ra[:len(ra) - L], rb[L:]) if L >= 0 else (ra[-L:], rb[:len(rb) + L])
        ok = np.isfinite(x) & np.isfinite(y)
        if ok.sum() < MIN_OVERLAP or x[ok].std() == 0 or y[ok].std() == 0:
            continue
        c = float(np.corrcoef(x[ok], y[ok])[0, 1])
        if best[1] is None or c > best[1]:
            best = (L, c)
    return best


def judge(a: np.ndarray, b: np.ndarray) -> dict:
    """Decide one pair (a, b). Returns up/down as 'a'/'b' plus the evidence, or a reason it was rejected."""
    ma, mb = np.nanmedian(a) if np.isfinite(a).any() else np.nan, np.nanmedian(b) if np.isfinite(b).any() else np.nan
    if np.isnan(ma) or np.isnan(mb):
        return {"reason": "no history"}
    drop = float(ma - mb)
    out = {"drop_m": round(drop, 2)}
    if abs(drop) < MIN_DROP_M:
        return out | {"reason": "flat: can't tell direction"}
    out["drop_m"] = round(abs(drop), 2)             # upstream minus downstream
    up, down = (a, b) if drop > 0 else (b, a)
    lag, c = best_lag(up, down, range(0, MAX_LAG_H + 1))
    any_lag, any_c = best_lag(up, down, range(-MAX_LAG_H, MAX_LAG_H + 1))
    out |= {"lag_h": lag, "corr": c and round(c, 2), "best_any_lag_h": any_lag}
    if c is None:
        return out | {"reason": "too little overlap"}
    if c < MIN_CORR:
        return out | {"reason": "weak correlation"}
    if any_lag is not None and any_lag < 0 and any_c - c > 0.05:
        return out | {"reason": "lag points the other way"}
    return out | {"up": "a" if drop > 0 else "b"}


def reduce(edges: dict[tuple[int, int], dict]) -> set[tuple[int, int]]:
    """Edges implied by a longer path of other edges."""
    out_of: dict[int, set[int]] = {}
    for u, v in edges:
        out_of.setdefault(u, set()).add(v)

    def reachable(u, v, skip):
        seen, stack = set(), [w for w in out_of.get(u, ()) if (u, w) != skip]
        while stack:
            w = stack.pop()
            if w == v:
                return True
            if w not in seen:
                seen.add(w)
                stack += list(out_of.get(w, ()))
        return False

    return {e for e in edges if reachable(e[0], e[1], e)}


def build(year: int) -> None:
    with connect() as conn:
        st = stations(conn)
        pairs = candidates(st)
        series: dict[int, np.ndarray] = {}

        def s(sid):
            if sid not in series:
                series[sid] = hourly(conn, sid, year)
            return series[sid]

        review, edges = [], {}
        for a, b in sorted(pairs):
            v = judge(s(a), s(b))
            u, d = (a, b) if v.get("up") == "a" else (b, a)
            km = round(_km((st[a]["lat"], st[a]["lon"]), (st[b]["lat"], st[b]["lon"])), 1)
            row = {"upstream_id": u, "downstream_id": d, "up_code": st[u]["code"], "down_code": st[d]["code"],
                   "river": st[a]["river"], "province_up": st[u]["province"], "province_down": st[d]["province"],
                   "km": km, **{k: v.get(k) for k in ("drop_m", "lag_h", "corr", "best_any_lag_h")},
                   "decision": "edge" if "up" in v else v["reason"]}
            review.append(row)
            if "up" in v:
                edges[(u, d)] = row
        for e in reduce(edges):
            edges.pop(e)["decision"] = "redundant (implied by a chain)"

    REVIEW_CSV.parent.mkdir(parents=True, exist_ok=True)
    with open(REVIEW_CSV, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(review[0]), lineterminator="\n")
        w.writeheader()
        w.writerows(review)

    with open(EDGES_CSV, encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if not r["note"].startswith("auto:")]
    have = {(int(r["upstream_id"]), int(r["downstream_id"])) for r in rows}
    rows += [{"upstream_id": u, "downstream_id": d, "approx_travel_hours": e["lag_h"],
              "note": f"auto: {e['up_code']} -> {e['down_code']} {e['river']} (corr {e['corr']}, drop {e['drop_m']} m)"}
             for (u, d), e in sorted(edges.items()) if (u, d) not in have]
    with open(EDGES_CSV, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["upstream_id", "downstream_id", "approx_travel_hours", "note"], lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    counts: dict[str, int] = {}
    for r in review:
        counts[r["decision"]] = counts.get(r["decision"], 0) + 1
    print(f"{len(pairs)} candidate pairs → {len(edges)} edges; {counts}", flush=True)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("cmd", choices=["fetch", "build"])
    p.add_argument("--year", type=int, default=2025)
    p.add_argument("--workers", type=int, default=4)
    args = p.parse_args()
    if args.cmd == "fetch":
        with connect() as conn:
            ids = {i for pair in candidates(stations(conn)) for i in pair}
            fetch_years(conn, ids, [args.year], args.workers)
    else:
        build(args.year)


if __name__ == "__main__":
    main()
