"""GISTDA satellite flood extent → flood_cells. The only module that downloads GISTDA's flood layers.

    python -m ingest.gistda_flood                  # both layers once (3-day, then 7-day)
    python -m ingest.gistda_flood --period 3days
    python -m ingest.gistda_flood --force          # re-store both even if unchanged (after a parser fix)
    python -m ingest.gistda_flood --every 10800    # loop; the API starts this as its own low-priority process
                                                   # when SCHEDULER=1 and a key is set (supervise below)

GET https://api-gateway.gistda.or.th/api/2.0/resources/features/flood/{3days|7days}?limit=&offset=
(OGC API Features, needs GISTDA_API_KEY). One GeoJSON feature per flooded H3 cell (res 9, ~0.1 km²).
The old gi-service/v1.0/disasters/flood-extent-1day point query is dead. What we know about this one comes from
other projects' write-ups (Oct 2026), not yet from a response of ours:
- The key goes in the `API-Key` header, never in the URL: the response's `links` echo the request URL.
- Geometry always comes back (skipGeometry / properties are ignored), ~3 KB a cell. The 3-day layer has held ~80k
  cells and the 7-day ~110k, so a full download is a few hundred MB: pages of 1,000 by offset, and a layer whose
  first page has not changed is not downloaded again.
- The 1-day and 3-day layers are sometimes empty.
- Property names are not documented. Each field is looked for under the names reported so far (the H3 index in any
  property that holds one, else from the geometry).
Every stored layer is also kept (copy plan phase 4): all its pages, gzipped with the key removed, under
var/raw/gistda/flood/<period>/<YYYYmmdd_HHMMSS_micros>/ for GISTDA_SNAPSHOT_KEEP_DAYS (default 90), so the parser
can be checked against a real response and a layer re-read after a parser fix; and its cells in
flood_cells_history, one copy per period per day, kept for good.
"""
import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import h3
import httpx
from psycopg.types.json import Jsonb

from common.config import RAW_DIR, ROOT
from ingest.thaiwater import BKK

BASE = "https://api-gateway.gistda.or.th/api/2.0/resources/features/flood"
PERIODS = ("3days", "7days")
PAGE = 1000
MIN_INTERVAL_S = 1.0
MAX_CELLS = 400_000                     # far above any layer seen: past this, offset is being ignored
REFETCH_ANYWAY = timedelta(hours=24)    # an unchanged first page is trusted for at most a day
CELL_RES = 9                            # GISTDA's grid, used only when a feature carries no H3 index
PARENT_RES = (5, 6, 7, 8)
RAI_PER_KM2 = 625
TO_KM2 = {"km2": 1.0, "rai": 1 / RAI_PER_KM2, "m2": 1e-6}
SNAPSHOT_KEEP_DAYS = int(os.environ.get("GISTDA_SNAPSHOT_KEEP_DAYS", "90"))   # raw pages of stored layers
SNAPSHOT_NAME = "%Y%m%d_%H%M%S_%f"              # sorts by time

H3_KEYS = ("h3_address", "h3", "h3_index", "h3index", "h3_id", "h3id", "hex_id", "hexid", "cell_id", "grid_id", "index")
# (property, unit). None = no unit in the name: read with area_unit() over the whole layer.
# f_area is GISTDA's flooded part of the cell in m² (seen 2026-10-19); h3_area next to it is the whole cell, so it
# comes last: read as a flooded area it makes every cell 100% flooded.
AREA_KEYS = (("f_area", "m2"), ("area_km2", "km2"), ("flood_km2", "km2"), ("area_sqkm", "km2"), ("area_rai", "rai"),
             ("flood_rai", "rai"), ("rai", "rai"), ("area_m2", "m2"), ("area_sqm", "m2"),
             ("flood_area", None), ("h3_area", None), ("area", None))
NAME_KEYS = {"province": ("pv_tn", "pv_name", "pv_th", "province_name", "prov_nam_t", "province"),
             "amphoe": ("ap_tn", "ap_name", "ap_th", "district_name", "amp_nam_t", "amphoe", "district"),
             "tambon": ("tb_tn", "tb_name", "tb_th", "subdistrict_name", "tam_nam_t", "tambon", "subdistrict")}
CODE_KEYS = {"pv_code": ("pv_idn", "pv_code", "province_code", "prov_code"),
             "ap_code": ("ap_idn", "ap_code", "district_code", "amp_code"),
             "tb_code": ("tb_idn", "tb_code", "subdistrict_code", "tam_code")}
# Counts of what lies in the flooded cell. Names as other projects report them; only plain counts are read (road
# length and rice area stay in props until a real response shows their units).
IMPACT_KEYS = {"buildings": ("building", "buildings", "num_building", "bldg"),
               "population": ("population", "pop", "pop_total"),
               "schools": ("school", "schools", "num_school"),
               "hospitals": ("hospital", "hospitals", "num_hospital")}
DATE_KEYS = ("date", "flood_date", "acq_date", "acquired", "datetime", "obs_date", "image_date", "update_date",
             "updated_at", "last_update", "created_at", "publish_date")
# GISTDA sends no date field: the scenes are named in file_name, "cm2_20261018_0607, cm4_20261019_0627". Their times
# are Thai local (06:07 / 17:10 are the dawn and dusk passes of a sun-synchronous radar satellite over Thailand).
SCENE_DATE = re.compile(r"(\d{8})_(\d{4})")
NAME_PREFIX = re.compile(r"^(จังหวัด|อำเภอ|ตำบล|เขต|แขวง|จ\.|อ\.|ต\.)\s*")


class GistdaError(RuntimeError):
    pass


# --- parsing (pure) ----------------------------------------------------------------------------------

def _num(v) -> float | None:
    try:
        return None if v is None or v == "" or isinstance(v, bool) else float(v)
    except (TypeError, ValueError):
        return None


def _cell(v) -> str | None:
    """An H3 cell index as a string, from the hex string or the 64-bit integer form; None if it isn't one."""
    try:
        s = h3.int_to_str(v) if isinstance(v, int) and not isinstance(v, bool) else str(v).strip().lower()
        return s if h3.is_valid_cell(s) else None
    except (TypeError, ValueError, OverflowError, h3.H3BaseException):
        return None


def _first(low: dict, keys) -> tuple[str, object] | None:
    for k in keys:
        if low.get(k) not in (None, ""):
            return k, low[k]
    return None


def _centroid(geom) -> tuple[float, float] | None:
    """(lat, lon): mean of the outer ring's vertices, fine for a hexagon or a small clipped patch."""
    try:
        ring = {"Polygon": lambda c: c[0], "MultiPolygon": lambda c: c[0][0]}[geom["type"]](geom["coordinates"])
        pts = ring[:-1] if len(ring) > 1 and ring[0] == ring[-1] else ring
        return sum(p[1] for p in pts) / len(pts), sum(p[0] for p in pts) / len(pts)
    except (KeyError, TypeError, IndexError, ZeroDivisionError):
        return None


def _when(v) -> datetime | None:
    """A date as GISTDA might send it: ISO text (Thai time when no offset, Buddhist years too) or epoch s/ms."""
    if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 1e9:
        return datetime.fromtimestamp(v / 1000 if v > 1e12 else v, timezone.utc)
    if not isinstance(v, str):
        return None
    s = v.strip().replace("Z", "+00:00").replace("/", "-")
    try:
        d = datetime.fromisoformat(s.replace(" ", "T", 1))
    except ValueError:
        return None
    if d.year > 2400:                        # พ.ศ.
        d = d.replace(year=d.year - 543)
    return d if d.tzinfo else d.replace(tzinfo=BKK)


def _name(v) -> str | None:
    """'อำเภอบางบาล' → 'บางบาล' (the UI adds อ./เขต itself)."""
    if v in (None, ""):
        return None
    return NAME_PREFIX.sub("", str(v).strip()) or None


def parse_feature(f: dict) -> dict | None:
    """One GeoJSON feature → a cell dict, or None when neither an H3 index nor a geometry places it.
    raw_area keeps GISTDA's figure; area_unit says how to read it (None: decide over the whole layer)."""
    props = f.get("properties") if isinstance(f, dict) else None
    low = {str(k).lower(): v for k, v in (props or {}).items()}
    used: set[str] = set()
    cell = None
    if hit := _first(low, H3_KEYS):
        if cell := _cell(hit[1]):
            used.add(hit[0])
    if cell is None:                         # any property that holds an H3 index
        for k, v in low.items():
            if isinstance(v, (str, int)) and (c := _cell(v)) and h3.get_resolution(c) >= 5:
                cell = c
                used.add(k)
                break
    if cell is None:
        if not (c := _centroid(f.get("geometry"))):
            return None
        cell = h3.latlng_to_cell(c[0], c[1], CELL_RES)
    lat, lon = h3.cell_to_latlng(cell)

    raw_area, unit = None, None
    for k, u in AREA_KEYS:
        if (a := _num(low.get(k))) is not None and a >= 0:
            raw_area, unit = a, u
            used.add(k)
            break

    out = {"h3": cell, "lat": lat, "lon": lon, "raw_area": raw_area, "area_unit": unit, "observed_at": None}
    for field, keys in NAME_KEYS.items():
        hit = _first(low, keys)
        out[field] = _name(hit[1]) if hit else None
        used.update([hit[0]] if hit else [])
    for field, keys in CODE_KEYS.items():
        hit = _first(low, keys)
        out[field] = str(hit[1]).strip() if hit else None
        used.update([hit[0]] if hit else [])
    tb = out["tb_code"]
    if tb and len(tb) == 6 and tb.isdigit():  # tb_idn = province 2 + amphoe 2 + tambon 2 digits
        out["pv_code"] = out["pv_code"] or tb[:2]
        out["ap_code"] = out["ap_code"] or tb[:4]
    for field, keys in IMPACT_KEYS.items():
        hit = _first(low, keys)
        n = _num(hit[1]) if hit else None
        out[field] = n if n is not None and n >= 0 else None
        used.update([hit[0]] if out[field] is not None else [])
    if hit := _first(low, DATE_KEYS):
        out["observed_at"] = _when(hit[1])
        used.add(hit[0])
    elif isinstance(low.get("file_name"), str) and (scenes := SCENE_DATE.findall(low["file_name"])):
        d, t = max(scenes)                   # the newest scene the cell was seen in
        out["observed_at"] = datetime.strptime(d + t, "%Y%m%d%H%M").replace(tzinfo=BKK)
        used.add("file_name")
    # Everything else as sent (impact counts, scenes), scalars only. URLs are dropped: links can carry the key.
    out["props"] = {k: v for k, v in (props or {}).items() if str(k).lower() not in used
                    and (v is None or isinstance(v, (bool, int, float)) or (isinstance(v, str) and len(v) <= 200
                                                                            and not v.startswith("http")))}
    return out


def area_unit(cells: list[dict]) -> str:
    """How to read area figures whose name gives no unit, judged over the whole layer: the largest figure against
    its own H3 cell's area. Any real layer has fully flooded cells, so the largest is about one cell: ~1 in km²,
    ~625 in rai, ~10⁶ in m². 'cell' (use the cell's area) when there is no figure or it fits none of these."""
    ratios = [c["raw_area"] / h3.cell_area(c["h3"], "km^2") for c in cells
              if c["raw_area"] is not None and c["area_unit"] is None]
    top = max(ratios, default=None)
    if top is None:
        return "cell"
    for unit, limit in (("km2", 1.5), ("rai", 1.5 * RAI_PER_KM2), ("m2", 1.5e6)):
        if top <= limit:
            return unit
    return "cell"


def finish(cells: list[dict]) -> tuple[list[dict], str]:
    """Parsed cells → flood_cells rows: one per H3 cell, flooded area in km² (never more than the cell itself),
    parent cells for zoomed-out maps. Returns (rows, how unit-less area figures were read)."""
    by_cell: dict[str, dict] = {}
    for c in cells:                          # a cell can come twice if the layer shifts between pages
        old = by_cell.get(c["h3"])
        if old is None or (c["raw_area"] or 0) > (old["raw_area"] or 0):
            by_cell[c["h3"]] = c
    guessed = area_unit(list(by_cell.values()))
    rows = []
    for c in by_cell.values():
        full = h3.cell_area(c["h3"], "km^2")
        unit = c["area_unit"] or guessed
        area = c["raw_area"] * TO_KM2[unit] if c["raw_area"] is not None and unit in TO_KM2 else full
        res = h3.get_resolution(c["h3"])
        parents = {f"h3_r{p}": h3.cell_to_parent(c["h3"], p) if p < res else c["h3"] for p in PARENT_RES}
        rows.append({**c, **parents, "area_km2": min(area, full)})
    return rows, guessed


# --- fetching ----------------------------------------------------------------------------------------

def _scrub(text: str, key: str | None) -> str:
    return text.replace(key, "***") if key else text


def _client(key: str) -> httpx.Client:
    return httpx.Client(base_url=BASE, timeout=httpx.Timeout(120, connect=20),
                        headers={"API-Key": key, "User-Agent": "NamMa flood assistant (hackathon)"})


def fetch_page(client: httpx.Client, period: str, offset: int, attempts: int = 3) -> dict:
    for i in range(attempts):
        try:
            r = client.get(f"/{period}", params={"limit": PAGE, "offset": offset})
        except httpx.TransportError:
            if i == attempts - 1:
                raise
            time.sleep(10 * (i + 1))
            continue
        if r.status_code in (401, 403, 407):
            raise GistdaError(f"GISTDA refused the key ({r.status_code}): {r.text[:200]}")
        if r.status_code >= 500 and i < attempts - 1:
            time.sleep(10 * (i + 1))
            continue
        r.raise_for_status()
        body = r.json()
        if not isinstance(body, dict) or not isinstance(body.get("features"), list):
            raise GistdaError(f"not a FeatureCollection: {str(body)[:200]}")
        return body
    raise GistdaError("unreachable")


def _has_next(page: dict) -> bool:
    links = page.get("links")
    return isinstance(links, list) and any(isinstance(l, dict) and l.get("rel") == "next" for l in links)


def fetch_layer(client: httpx.Client, period: str, first: dict):
    """Every page of a layer, starting with `first` (offset 0). It ends at numberMatched when the layer gives one,
    else at a short page with no `next` link (a missing link alone proves nothing). A layer that stops short of its
    numberMatched raises, so a partial layer is never stored."""
    page, offset = first, 0
    total = first.get("numberMatched") if isinstance(first.get("numberMatched"), int) else None
    head = _head(first)
    while True:
        yield page
        n = len(page["features"])
        offset += n
        if total is not None:
            if offset >= total:
                return
            if n == 0:
                raise GistdaError(f"{period}: the layer ended at {offset} of its {total} cells")
        elif n == 0 or (n < PAGE and not _has_next(page)):
            return
        if offset >= MAX_CELLS:
            raise GistdaError(f"{period}: more than {MAX_CELLS} cells, offset looks ignored")
        time.sleep(MIN_INTERVAL_S)
        page = fetch_page(client, period, offset)
        if head and _head(page) == head:
            raise GistdaError(f"{period}: page at offset {offset} repeats the first page, offset looks ignored")


def _head(page: dict) -> str:
    return json.dumps(page["features"][:3], sort_keys=True, default=str)


def fingerprint(first: dict) -> str:
    """Of the first page's cells (geometry too) and the layer size; not of timeStamp/links, which change on every
    request."""
    body = json.dumps([first.get("numberMatched"), first["features"]], sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(body.encode()).hexdigest()[:32]


def snapshot_dir(period: str) -> Path:
    return RAW_DIR / "gistda" / "flood" / period


def save_page(d: Path, offset: int, page: dict, key: str | None) -> None:
    """One page, gzipped, without `links` (they echo the request URL) and with the key removed."""
    body = _scrub(json.dumps({k: v for k, v in page.items() if k != "links"}, ensure_ascii=False), key)
    with gzip.open(d / f"{offset:07d}.json.gz", "wt", encoding="utf-8") as f:
        f.write(body)


def prune_snapshots(period: str, now: datetime) -> None:
    """Drop snapshots older than SNAPSHOT_KEEP_DAYS, and any left part-way by a run that was stopped."""
    cutoff = (now - timedelta(days=SNAPSHOT_KEEP_DAYS)).strftime(SNAPSHOT_NAME)
    for d in snapshot_dir(period).iterdir():
        if d.is_dir() and (d.suffix == ".partial" or d.name < cutoff):
            shutil.rmtree(d, ignore_errors=True)


# --- storing -----------------------------------------------------------------------------------------

COLS = ("period", "h3", "h3_r5", "h3_r6", "h3_r7", "h3_r8", "lat", "lon", "area_km2", "pv_code", "ap_code", "tb_code",
        "province", "amphoe", "tambon", "observed_at", "buildings", "population", "schools", "hospitals", "props")
HISTORY_COLS = ("h3", "lat", "lon", "area_km2", "pv_code", "ap_code", "tb_code", "observed_at", "buildings",
                "population", "schools", "hospitals")


def store(conn, period: str, rows: list[dict], fp: str | None, unit: str) -> dict:
    """Replace the period's cells with this layer, log the fetch, and make the layer today's copy in
    flood_cells_history. The caller commits."""
    conn.execute("DELETE FROM flood_cells WHERE period = %s", (period,))
    with conn.cursor() as cur, cur.copy(f"COPY flood_cells ({', '.join(COLS)}) FROM STDIN") as copy:
        for r in rows:
            copy.write_row([period, *(r[c] for c in COLS[1:-1]), Jsonb(r["props"])])
    by_province: dict[str, float] = defaultdict(float)
    for r in rows:
        by_province[r["province"] or r["pv_code"] or "?"] += r["area_km2"]
    seen = [r["observed_at"] for r in rows if r["observed_at"]]
    summary = {"period": period, "cells": len(rows), "area_km2": round(sum(r["area_km2"] for r in rows), 4),
               "observed_from": min(seen, default=None), "observed_to": max(seen, default=None)}
    fetch_id = conn.execute(
        "INSERT INTO flood_fetches (period, cells, area_km2, observed_from, observed_to, fingerprint, "
        "area_unit, by_province) VALUES (%(period)s, %(cells)s, %(area_km2)s, %(observed_from)s, "
        "%(observed_to)s, %(fp)s, %(unit)s, %(by_province)s) RETURNING id",
        {**summary, "fp": fp, "unit": unit,
         "by_province": Jsonb({k: round(v, 4) for k, v in sorted(by_province.items())})}).fetchone()[0]
    day = datetime.now(BKK).date()
    cols = ", ".join(HISTORY_COLS)
    conn.execute("DELETE FROM flood_cells_history WHERE day = %s AND period = %s", (day, period))
    conn.execute(f"INSERT INTO flood_cells_history (day, period, fetch_id, {cols}) "
                 f"SELECT %s, period, %s, {cols} FROM flood_cells WHERE period = %s", (day, fetch_id, period))
    return {**summary, "day": day}


def run_once(conn, period: str, key: str | None = None, client: httpx.Client | None = None,
             force: bool = False) -> dict | None:
    """Download one layer if it changed (or `force`: after a parser fix) and store it. Returns the stored summary, or
    None when unchanged. Any failure raises and leaves the stored layer as it was."""
    key = key or os.environ.get("GISTDA_API_KEY")
    if not key and client is None:
        raise GistdaError("GISTDA_API_KEY is not set")
    last = conn.execute("SELECT fingerprint, fetched_at FROM flood_fetches WHERE period = %s "
                        "ORDER BY fetched_at DESC LIMIT 1", (period,)).fetchone()
    conn.rollback()                          # no transaction left open during a download of several minutes
    own = client is None
    client = client or _client(key)
    snap = None
    try:
        first = fetch_page(client, period, 0)
        fp = fingerprint(first)
        if not force and last and last[0] == fp and datetime.now(timezone.utc) - last[1] < REFETCH_ANYWAY:
            return None
        now = datetime.now(BKK)
        snapshot_dir(period).mkdir(parents=True, exist_ok=True)
        prune_snapshots(period, now)
        snap = snapshot_dir(period) / f"{now.strftime(SNAPSHOT_NAME)}.partial"
        snap.mkdir()
        cells, offset = [], 0
        for page in fetch_layer(client, period, first):
            save_page(snap, offset, page, key)
            offset += len(page["features"])
            cells += [c for f in page["features"] if (c := parse_feature(f))]
        rows, unit = finish(cells)
        summary = store(conn, period, rows, fp, unit)
        conn.commit()
        snap = snap.rename(snap.with_suffix(""))     # whole and stored: only now a snapshot
        return summary
    finally:
        if own:
            client.close()
        if snap is not None and snap.suffix == ".partial":
            shutil.rmtree(snap, ignore_errors=True)


def run_all(periods=PERIODS, force: bool = False) -> None:
    from common.db import connect
    key = os.environ.get("GISTDA_API_KEY")
    for period in periods:
        started = time.monotonic()
        try:
            with connect() as conn:
                s = run_once(conn, period, key, force=force)
            print(f"gistda {period}: " + ("unchanged" if s is None else
                  f"{s['cells']} cells, {s['area_km2']:.1f} km² in {time.monotonic() - started:.0f} s"), flush=True)
        except Exception as e:               # keep the stored layer; water data does not depend on this
            print(f"gistda {period} skipped: {_scrub(repr(e), key)}", flush=True)


def loop(every: int, periods=PERIODS) -> None:
    while True:
        run_all(periods)
        time.sleep(every)


def supervise(every: int) -> None:
    """For the API (api/main.py): run `loop` in its own process at low priority and start it again if it ends. A
    7-day layer is minutes of download and parsing, which in the API's own process slowed the site. The child
    shares the API's systemd unit, so stopping or restarting the API stops it too."""
    cmd = ["nice", "-n", "10", sys.executable, "-m", "ingest.gistda_flood", "--every", str(every)]
    while True:
        code = subprocess.Popen(cmd, cwd=ROOT).wait()
        print(f"gistda fetcher exited with {code}, starting it again in 60 s", flush=True)
        time.sleep(60)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--period", choices=PERIODS, help="one layer; default both")
    p.add_argument("--every", type=int, help="seconds between runs; omit to run once")
    p.add_argument("--force", action="store_true", help="store the layer even if unchanged (after a parser fix)")
    args = p.parse_args()
    periods = (args.period,) if args.period else PERIODS
    loop(args.every, periods) if args.every else run_all(periods, force=args.force)


if __name__ == "__main__":
    main()
