"""Backtest the rule-based tiers on the Sept–Oct 2026 event (CLAUDE.md §9, demo step 5).

Replays agent.risk.assess hour by hour on ThaiWater history for the corridor stations that have
edges, and reports warning lead time before each over-bank onset. Persistence ("what we see now")
has zero lead time by definition, so any positive lead is what the rules add.

    python -m model.backtest --from 2026-09-01        # prints a table, writes var/backtest_hourly.csv
"""
import argparse
import csv
from datetime import date, datetime, timedelta

from agent import risk
from common.config import ROOT
from ingest import thaiwater

EDGES_CSV = ROOT / "data" / "station_edges.csv"
OUT = ROOT / "var" / "backtest_hourly.csv"
CODES = {2795: "C.2", 2744: "C.13", 2723: "C.3", 2626: "C.7A", 2609: "C.35", 2599: "C.12", 2611: "C.36",
         2624: "S.26", 2607: "S.5"}
ONSET_QUIET_H = 24      # an onset counts only after a day below bank
LOOKBACK_H = 72         # how far back a warning still counts as "for this event"


def hourly(rows: list[dict]) -> dict[datetime, tuple[float, float | None]]:
    out = {}
    for r in rows:
        out[r["ts"].replace(minute=0, second=0)] = (r["level_msl"], r["diff_wl_bank"])
    return out


def replay(sid: int, h: dict, upstream: list[dict], hours: list[datetime]) -> dict[datetime, str]:
    tiers = {}
    for t in hours:
        if t not in h:
            continue
        window = [(x, h[x][0], h[x][1], None) for x in (t - timedelta(hours=i) for i in range(12, -1, -1)) if x in h]
        up_over = [1 for u in upstream if t in u and u[t][1] is not None and u[t][1] >= 0]
        tiers[t] = risk.assess(sid, window, up_over, t).risk_tier
    return tiers


def onsets(h: dict, hours: list[datetime]) -> list[datetime]:
    out, below_since = [], None
    for t in hours:
        if t not in h or h[t][1] is None:
            continue
        if h[t][1] < 0:
            below_since = below_since or t
        else:
            if below_since and t - below_since >= timedelta(hours=ONSET_QUIET_H):
                out.append(t)
            below_since = None
    return out


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--from", dest="start", default="2026-09-01")
    p.add_argument("--to", dest="end", default=date.today().isoformat())
    args = p.parse_args()
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    with open(EDGES_CSV, encoding="utf-8") as f:
        edges = [(int(e["upstream_id"]), int(e["downstream_id"])) for e in csv.DictReader(f)]
    ids = sorted({i for e in edges for i in e})
    data = {sid: hourly(thaiwater.fetch_waterlevel_history(sid, start - timedelta(days=1), end)) for sid in ids}
    t0 = datetime(start.year, start.month, start.day, tzinfo=thaiwater.BKK)
    hours = [t0 + timedelta(hours=i) for i in range(int((datetime.now(thaiwater.BKK) - t0).total_seconds() // 3600))]

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["station", "ts", "level_msl", "diff_wl_bank", "tier"])
        print(f"{'station':<7} {'over-bank onset':<17} {'first watch':<17} {'first warning':<17} lead_h")
        for sid in ids:
            ups = [data[u] for u, d in edges if d == sid]
            tiers = replay(sid, data[sid], ups, hours)
            for t, tier in tiers.items():
                w.writerow([CODES.get(sid, sid), t.isoformat(), *data[sid][t], tier])
            for on in onsets(data[sid], hours):
                win = [t for t in tiers if on - timedelta(hours=LOOKBACK_H) <= t < on]
                fw = next((t for t in win if risk.rank(tiers[t]) >= risk.rank("watch")), None)
                fa = next((t for t in win if risk.rank(tiers[t]) >= risk.rank("warning")), None)
                lead = round((on - fa).total_seconds() / 3600) if fa else 0
                fmt = lambda t: f"{t:%Y-%m-%d %H:%M}" if t else "-"
                print(f"{CODES.get(sid, sid):<7} {fmt(on):<17} {fmt(fw):<17} {fmt(fa):<17} {lead}", flush=True)
    print(f"hourly tiers → {OUT}")


if __name__ == "__main__":
    main()
