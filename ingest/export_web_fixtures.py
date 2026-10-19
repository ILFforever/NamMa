"""Export real corridor data as fixtures for the web app's mock API.

    python -m ingest.export_web_fixtures            # live waterlevel_load + 7 days of history
    python -m ingest.export_web_fixtures --days 3

Writes stations.json and readings.json for the stations in data/corridor_stations.csv, and rivers.json (the
corridor's rivers plus the best-gauged others, grouped by ingest.rivers), into the namma-web
repo's src/api/mock/fixtures (default: the checkout next to this one, ../namma-web; or --out DIR). ~25 requests at the client's 1 req/s limit.

Risk here is PROVISIONAL: a simplified version of the CLAUDE.md §8 rules so the mock map
has tiers. Swap provisional_risk() for the real risk module once it exists.
"""
import argparse
import csv
import json
from datetime import date, datetime, timedelta
from pathlib import Path

from ingest import rivers, thaiwater

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT.parent / "namma-web" / "src" / "api" / "mock" / "fixtures"   # namma-web checkout next to this repo; --out overrides
RISING_CM_PER_HR = 0.5
WATCH_GAP_M = 0.5
WARNING_HOURS = 6.0
EXTRA_RIVERS = 4      # rivers.json: besides the corridor's, this many of the best-gauged others
SUMMARY_KEYS = ("id", "oldcode", "name_th", "name_en", "lat", "lon", "river", "province", "amphoe", "agency",
                "is_key_station", "stem_order", "role", "latest", "risk")


def provisional_risk(diff_wl_bank: float | None, trend_cm_per_hr: float | None,
                     upstream_over_bank: bool) -> tuple[str, float | None] | None:
    """(tier, hours_to_bank) or None when the bank gap is unknown."""
    if diff_wl_bank is None:
        return None
    rising = trend_cm_per_hr is not None and trend_cm_per_hr > RISING_CM_PER_HR
    hours_to_bank = -diff_wl_bank * 100 / trend_cm_per_hr if rising and diff_wl_bank < 0 else None
    if diff_wl_bank >= 0:
        return "danger", None
    if (hours_to_bank is not None and hours_to_bank < WARNING_HOURS) or upstream_over_bank:
        return "warning", hours_to_bank
    if rising and -diff_wl_bank < WATCH_GAP_M:
        return "watch", hours_to_bank
    return "normal", hours_to_bank


def trend_cm_per_hr(readings: list[dict], hours: float = 3.0) -> float | None:
    """Level change per hour between the latest reading and the one closest to `hours` earlier."""
    if len(readings) < 2:
        return None
    last = readings[-1]
    target = last["ts"] - timedelta(hours=hours)
    past = min(readings[:-1], key=lambda r: abs(r["ts"] - target))
    span_h = (last["ts"] - past["ts"]).total_seconds() / 3600
    if span_h < hours * 0.5 or abs(past["ts"] - target) > timedelta(minutes=45):
        return None
    return round((last["level_msl"] - past["level_msl"]) * 100 / span_h, 2)


def hourly(readings: list[dict]) -> list[dict]:
    """First reading in each clock hour (HII stations report every 10 min)."""
    out, seen = [], set()
    for r in sorted(readings, key=lambda r: r["ts"]):
        hour = r["ts"].replace(minute=0, second=0, microsecond=0)
        if hour not in seen:
            seen.add(hour)
            out.append(r)
    return out


def _r(v: float | None, digits: int = 3) -> float | None:
    return None if v is None else round(v, digits)


def load_corridor() -> dict[int, dict]:
    with open(ROOT / "data" / "corridor_stations.csv", encoding="utf-8") as f:
        return {int(row["id"]): row for row in csv.DictReader(f)}


def load_edges() -> list[dict]:
    with open(ROOT / "data" / "station_edges.csv", encoding="utf-8") as f:
        return [{"upstream_id": int(r["upstream_id"]), "downstream_id": int(r["downstream_id"]),
                 "approx_travel_hours": float(r["approx_travel_hours"]) if r["approx_travel_hours"] else None}
                for r in csv.DictReader(f)]


def _latest(lr: dict | None) -> dict | None:
    return None if lr is None else {
        "ts": lr["ts"].isoformat(),
        "level_msl": lr["level_msl"],
        "diff_wl_bank": _r(lr["diff_wl_bank"]),
        "situation_level": int(lr["situation_level"]) if lr["situation_level"] is not None else None,
        "discharge": lr["discharge"],
    }


def river_fixtures(all_stations: list[dict], all_latest: dict[int, dict], corridor_summaries: dict[int, dict],
                   now: datetime) -> list[dict]:
    """Rivers as GET /api/rivers/{key} returns them, minus what depends on the request (reason, distance_km,
    worst_tier: the mock works those out). Corridor stations reuse their stations.json summary; the others get
    the provisional tier from the bank gap alone (no history fetched for them, so no trend)."""
    placed = rivers.group([rivers.Station(s["id"], s["river"], s["lat"], s["lon"], s["min_bank"], s["agency"],
                                          all_latest[s["id"]]["ts"] if s["id"] in all_latest else None)
                           for s in all_stations], rivers.load_overrides(), now)
    by_key: dict[str, list[tuple[int, int]]] = {}
    for sid, p in placed.items():
        if p.river_order is not None:
            by_key.setdefault(p.river_key, []).append((p.river_order, sid))
    by_key = {k: [sid for _, sid in sorted(v)] for k, v in by_key.items() if len(v) >= 2}
    corridor_keys = sorted({placed[sid].river_key for sid in corridor_summaries if sid in placed} & set(by_key))
    others = sorted((k for k in by_key if k not in corridor_keys), key=lambda k: (-len(by_key[k]), k))
    info = {s["id"]: s for s in all_stations}

    def summary(sid: int) -> dict:
        if sid in corridor_summaries:
            return {k: corridor_summaries[sid][k] for k in SUMMARY_KEYS}
        s, lr = info[sid], all_latest.get(sid)
        risk = provisional_risk(lr["diff_wl_bank"] if lr else None, None, False)
        return {**{k: s[k] for k in SUMMARY_KEYS[:11]}, "stem_order": None, "role": None, "latest": _latest(lr),
                "risk": None if risk is None or lr is None else {
                    "tier": risk[0], "trend_cm_per_hr": None, "hours_to_bank": None,
                    "computed_at": lr["ts"].isoformat()}}

    out = []
    for key in corridor_keys + others[:EXTRA_RIVERS]:
        ids = by_key[key]
        basins = list(dict.fromkeys(info[i]["basin"] for i in ids if info[i]["basin"]))
        provinces = list(dict.fromkeys(info[i]["province"] for i in ids if info[i]["province"]))
        out.append({"key": key, "name_th": key.split("@")[0], "basins": basins, "provinces": provinces,
                    "stations": [summary(i) for i in ids]})
    return out


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--out", type=Path, default=OUT, help="namma-web's src/api/mock/fixtures directory")
    args = p.parse_args()
    out = args.out

    corridor = load_corridor()
    edges = load_edges()

    all_stations, latest = thaiwater.parse_waterlevel_load(thaiwater.fetch_waterlevel_load())
    all_latest: dict[int, dict] = {}
    for r in latest:
        if r["station_id"] not in all_latest or r["ts"] > all_latest[r["station_id"]]["ts"]:
            all_latest[r["station_id"]] = r
    stations = {s["id"]: s for s in all_stations if s["id"] in corridor}
    latest = {r["station_id"]: r for r in latest if r["station_id"] in corridor}
    missing = sorted(set(corridor) - set(stations))
    if missing:
        print(f"not in waterlevel_load: {missing}")

    today = date.today()
    history: dict[int, list[dict]] = {}
    for sid in stations:
        rows = thaiwater.fetch_waterlevel_history(sid, today - timedelta(days=args.days), today)
        if sid in latest and (not rows or latest[sid]["ts"] > rows[-1]["ts"]):
            rows.append(latest[sid])
        history[sid] = sorted(rows, key=lambda r: r["ts"])
        print(f"{stations[sid]['oldcode']:>7} {len(rows):5d} readings")

    trends = {sid: trend_cm_per_hr(rows) for sid, rows in history.items()}
    current_diff = {sid: (latest[sid]["diff_wl_bank"] if sid in latest else
                          (history[sid][-1]["diff_wl_bank"] if history[sid] else None))
                    for sid in stations}

    out_stations = []
    for sid, s in stations.items():
        row = corridor[sid]
        upstream_over = any(current_diff.get(e["upstream_id"]) is not None and current_diff[e["upstream_id"]] > 0
                            for e in edges if e["downstream_id"] == sid)
        lr = latest.get(sid)
        risk = provisional_risk(current_diff[sid], trends[sid], upstream_over)
        out_stations.append({
            **{k: s[k] for k in ("id", "oldcode", "name_th", "name_en", "lat", "lon", "river", "province",
                                 "amphoe", "agency", "is_key_station", "left_bank", "right_bank", "min_bank",
                                 "ground_level", "critical_level_msl")},
            "stem_order": int(row["stem_order"]) if row["stem_order"] else None,
            "role": row["role"] or None,
            "latest": _latest(lr),
            "risk": None if risk is None or lr is None else {
                "tier": risk[0],
                "trend_cm_per_hr": trends[sid],
                "hours_to_bank": _r(risk[1], 1),
                "computed_at": lr["ts"].isoformat(),
            },
        })
    out_stations.sort(key=lambda s: (s["stem_order"] is None, s["stem_order"] or 0, s["id"]))

    readings = {str(sid): [[r["ts"].isoformat(), r["level_msl"], _r(r["diff_wl_bank"]), r["discharge"]]
                           for r in hourly(rows)]
                for sid, rows in history.items()}

    ingested_at = max((r["ts"] for r in latest.values()), default=None)
    out.mkdir(parents=True, exist_ok=True)
    meta = {"generated_at": datetime.now(thaiwater.BKK).isoformat(timespec="seconds"),
            "ingested_at": ingested_at.isoformat() if ingested_at else None,
            "source": "ThaiWater (HII) waterlevel_load + waterlevel_graph",
            "risk": "provisional, see ingest/export_web_fixtures.py"}
    (out / "stations.json").write_text(
        json.dumps({"meta": meta, "stations": out_stations, "edges": edges}, ensure_ascii=False, indent=1),
        encoding="utf-8")
    (out / "readings.json").write_text(
        json.dumps({"meta": meta, "columns": ["ts", "level_msl", "diff_wl_bank", "discharge"],
                    "readings": readings}, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8")
    river_list = river_fixtures(all_stations, all_latest, {s["id"]: s for s in out_stations},
                                datetime.now(thaiwater.BKK))
    (out / "rivers.json").write_text(
        json.dumps({"meta": meta, "rivers": river_list}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"wrote {len(river_list)} rivers: " + ", ".join(f"{r['key']} ({len(r['stations'])})" for r in river_list))
    tiers = {}
    for s in out_stations:
        t = s["risk"]["tier"] if s["risk"] else "unknown"
        tiers[t] = tiers.get(t, 0) + 1
    print(f"wrote {len(out_stations)} stations, tiers {tiers} -> {out}")


if __name__ == "__main__":
    main()
