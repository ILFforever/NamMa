"""Rivers for the web's river charts (docs/rivers-api.md): every ThaiWater station put on a river, upstream first.

    python -m ingest.rivers         # recompute now (agent.jobs runs it after every ingest)

group() is pure; run_once() reads stations from the DB and writes river_key, river_order and same_place_as.
1. Group by ThaiWater's river name, split into clusters of stations that each lie within CLUSTER_KM of another
   (separates generic names on different streams, e.g. คลองใหญ่ in three basins, but keeps a river whole
   across its dam).
2. Stations under SAME_PLACE_KM apart are one place: the preferred one (RID, fresh) keeps the place,
   the others get same_place_as and no river_order (C.35 and CPY011 are 10 m apart).
3. Upstream first by bank elevation (min_bank, highest first): rank correlation 0.977–0.997 with the true
   order on six rivers in the live data, 2026-10-19.
data/river_overrides.csv beats all three, per station.
"""
import csv
from dataclasses import dataclass
from datetime import datetime, timedelta

from agent.geo import haversine_km
from common.config import ROOT
from common.db import connect

CLUSTER_KM = 150       # bridges a reservoir (Ping 113 km across Bhumibol, Nan 94 km across Sirikit); same-named streams in
                       # other basins are 500+ km apart (คลองใหญ่: Rayong, Phatthalung, Yala)
SAME_PLACE_KM = 3      # co-located RID/HII pairs are ≤ 2.5 km apart; the nearest distinct places on the Chao Phraya, 8 km
STALE = timedelta(hours=3)
AGENCY_RANK = {"RID": 0, "HII": 1}   # RID banks are the surveyed ones and the risk rules were tuned on RID
OVERRIDES_CSV = ROOT / "data" / "river_overrides.csv"


@dataclass(frozen=True)
class Station:
    id: int
    river: str | None
    lat: float | None
    lon: float | None
    min_bank: float | None
    agency: str | None
    latest_ts: datetime | None = None


@dataclass(frozen=True)
class Override:
    river_key: str | None = None    # put the station on this river instead of its ThaiWater name's
    sort_bank: float | None = None  # sort as if its bank were this high (fixes a swap without hand-numbering a river)
    exclude: bool = False


@dataclass(frozen=True)
class Placement:
    river_key: str
    river_order: int | None      # None for a station standing in for another (same_place_as)
    same_place_as: int | None


def _clusters(stations: list[Station]) -> list[list[Station]]:
    """Single-linkage clusters under CLUSTER_KM, each sorted by id. Stations on one river are few (≤ ~25)."""
    left = sorted(stations, key=lambda s: s.id)
    out = []
    while left:
        cluster = [left.pop(0)]
        frontier = list(cluster)
        while frontier:
            near = [s for s in left if any(haversine_km(f.lat, f.lon, s.lat, s.lon) < CLUSTER_KM for f in frontier)]
            left = [s for s in left if s not in near]
            cluster += near
            frontier = near
        out.append(sorted(cluster, key=lambda s: s.id))
    return out


def _preference(now: datetime):
    def key(s: Station):
        stale = s.latest_ts is None or s.latest_ts < now - STALE
        return (stale, AGENCY_RANK.get(s.agency or "", 2), s.id)
    return key


def group(stations: list[Station], overrides: dict[int, Override] | None = None,
          now: datetime | None = None) -> dict[int, Placement]:
    """Placement for every station that goes on a river; stations left out (no river name, no position,
    no usable bank, excluded) are absent. A river key is the river name, or name@<lowest station id> for
    the second and later clusters of one name, so it survives restarts and new stations elsewhere."""
    overrides = overrides or {}
    now = now or datetime.now().astimezone()
    none = Override()

    def bank(s: Station) -> float | None:
        o = overrides.get(s.id, none)
        return o.sort_bank if o.sort_bank is not None else s.min_bank

    rivers: dict[str, list[Station]] = {}
    by_name: dict[str, list[Station]] = {}
    for s in stations:
        o = overrides.get(s.id, none)
        b = bank(s)
        if o.exclude or s.lat is None or s.lon is None or b is None or b <= 0:
            continue
        if o.river_key:
            rivers.setdefault(o.river_key, []).append(s)
        elif s.river and s.river.strip():
            by_name.setdefault(s.river.strip(), []).append(s)
    for name, members in by_name.items():
        for i, c in enumerate(sorted(_clusters(members), key=lambda c: (-len(c), c[0].id))):
            rivers.setdefault(name if i == 0 else f"{name}@{c[0].id}", []).extend(c)

    out: dict[int, Placement] = {}
    for key, members in rivers.items():
        kept: list[Station] = []
        for s in sorted(members, key=_preference(now)):
            twin = next((k for k in kept if haversine_km(k.lat, k.lon, s.lat, s.lon) < SAME_PLACE_KM), None)
            if twin is None:
                kept.append(s)
            else:
                out[s.id] = Placement(key, None, twin.id)
        for i, s in enumerate(sorted(kept, key=lambda s: (-bank(s), s.id)), start=1):
            out[s.id] = Placement(key, i, None)
    return out


def load_overrides(path=OVERRIDES_CSV) -> dict[int, Override]:
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return {int(r["station_id"]): Override(
            river_key=(r.get("river_key") or "").strip() or None,
            sort_bank=float(r["sort_bank"]) if (r.get("sort_bank") or "").strip() else None,
            exclude=(r.get("exclude") or "").strip().lower() in ("1", "true", "yes"))
            for r in csv.DictReader(line for line in f if not line.startswith("#"))}


def run_once(conn) -> int:
    """Recompute every station's placement. Returns how many stations are on a river."""
    rows = conn.execute(
        "SELECT s.id, s.river, s.lat, s.lon, s.min_bank, s.agency, "
        "(SELECT max(ts) FROM waterlevel_readings w WHERE w.station_id = s.id) AS latest_ts "
        "FROM stations s").fetchall()
    stations = [Station(*r) for r in rows]
    placed = group(stations, load_overrides())
    with conn.cursor() as cur:
        cur.execute("UPDATE stations SET river_key = NULL, river_order = NULL, same_place_as = NULL "
                    "WHERE river_key IS NOT NULL")
        cur.executemany("UPDATE stations SET river_key = %s, river_order = %s, same_place_as = %s WHERE id = %s",
                        [(p.river_key, p.river_order, p.same_place_as, sid) for sid, p in placed.items()])
    return len(placed)


def main() -> None:
    with connect() as conn:
        n = run_once(conn)
        (rivers,) = conn.execute("SELECT count(DISTINCT river_key) FROM stations WHERE river_key IS NOT NULL").fetchone()
    print(f"rivers: {n} stations on {rivers} rivers", flush=True)


if __name__ == "__main__":
    main()
