"""GISTDA flood history 2011–2024 → flood_history_cells + flood_history_tambon. Copy plan phases 1–2.

    python -m ingest.gistda_history --province 14                  # Ayutthaya pilot
    python -m ingest.gistda_history --province 14 --show บางบาล     # and print that sub-district's years
    python -m ingest.gistda_history --province 14 --refetch        # download again (yearly refresh)

GET https://api-gateway.gistda.or.th/api/2.0/resources/features/flood-freq?pv_idn=&limit=&offset=
(OGC API Features, GISTDA_API_KEY in the API-Key header). One feature per flooded shape, with a flag per year
(y_2011 … y_2024), a frequency count, and ต./อ./จ. names and codes. What we measured from the VM (2026-10-19):
- By province / district / sub-district code a page takes 0.1–0.9 s; by map box 20 s to a time-out. So this job
  only ever asks by province code.
- skipGeometry=true returns nothing, so shapes come with their geometry and we turn them into H3 cells here.
- Ayutthaya 285,990 shapes, Chiang Mai 24,161, Bangkok 5,619; ~2.5 MB per 1,000 shapes.

Run it on its own, off-hours, never in the API process (the VM has 1.7 GB of memory): pages are read one at a time.
Every page is kept gzipped under var/raw/gistda/flood-freq/pv<code>/, which is also how a stopped run resumes: a
page already on disk is not downloaded again, and a rerun with every page on disk rebuilds the tables with no
GISTDA call. With OBS_BUCKET set, the pages also go to obs://<bucket>/gistda/flood-freq/pv<code>/ (only those not
there yet, or changed). OBS credentials come from the VM's IAM agency through the ECS metadata service, so there
is no AK/SK on the VM (CLAUDE.md §11).
Property names other than the year flags come from the 3/7-day layers' parser (ingest/gistda_flood.py), so a
province whose shapes carry no sub-district code still gets its cells; the summary counts such shapes.
"""
import argparse
import gzip
import json
import os
import re
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import h3
import httpx
from psycopg.types.json import Jsonb

from common.config import RAW_DIR
from ingest import gistda_flood as gf

BASE = "https://api-gateway.gistda.or.th/api/2.0/resources/features/flood-freq"
PAGE = 1000
WORKERS = int(os.environ.get("GISTDA_WORKERS", "4"))   # pages in flight at once
MIN_INTERVAL_S = 0.5                   # least gap between two request starts, across all workers
RATE_LIMIT_TRIES = 5                    # 429s in a row before giving up on a page
RATE_LIMIT_WAIT_S = 60                  # when a 429 carries no Retry-After
MAX_SHAPES = 1_500_000                  # far above any province measured: past this, offset is being ignored
RES = 9                                 # the same grid as the 3/7-day layers
FIRST_YEAR = 2011                       # bit 0 of a year mask
RAI_PER_KM2 = 625

OBS_PREFIX = "gistda/flood-freq"
OBS_DEFAULT_ENDPOINT = "https://obs.ap-southeast-2.myhuaweicloud.com"    # AP-Bangkok

YEAR_KEY = re.compile(r"^(?:y|yr|year|f)_?(\d{4})$")
FREQ_KEYS = ("freq", "frequency", "flood_freq", "freq_flood", "total", "count", "times")


class GistdaError(RuntimeError):
    pass


# --- parsing (pure) ----------------------------------------------------------------------------------

def _flag(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("1", "y", "yes", "true", "t")
    n = gf._num(v)
    return bool(v) if isinstance(v, bool) else n is not None and n > 0


def years_of(mask: int) -> list[int]:
    return [FIRST_YEAR + b for b in range(mask.bit_length()) if mask >> b & 1]


def parse_shape(f: dict) -> dict:
    """One flood-freq feature → {years (mask), freq_gistda, pv/ap/tb codes, province/amphoe/tambon, geometry}."""
    props = f.get("properties") if isinstance(f, dict) else None
    low = {str(k).lower(): v for k, v in (props or {}).items()}
    mask = 0
    for k, v in low.items():
        if (m := YEAR_KEY.match(k)) and _flag(v):
            y = int(m.group(1))
            y = y - 543 if y > 2400 else y               # พ.ศ.
            if FIRST_YEAR <= y < FIRST_YEAR + 31:
                mask |= 1 << (y - FIRST_YEAR)
    out = {"years": mask, "geometry": f.get("geometry") if isinstance(f, dict) else None}
    hit = gf._first(low, FREQ_KEYS)
    out["freq_gistda"] = gf._num(hit[1]) if hit else None
    out["area_rai"] = gf._num(low.get("area_rai"))    # GISTDA's own area; shapes are mostly far smaller than a cell
    for field, keys in gf.NAME_KEYS.items():
        hit = gf._first(low, keys)
        out[field] = gf._name(hit[1]) if hit else None
    for field, keys in gf.CODE_KEYS.items():
        hit = gf._first(low, keys)
        out[field] = str(hit[1]).strip() if hit else None
    tb = out["tb_code"]
    if tb and len(tb) == 6 and tb.isdigit():
        out["pv_code"] = out["pv_code"] or tb[:2]
        out["ap_code"] = out["ap_code"] or tb[:4]
    return out


def cells_of(geom) -> list[str]:
    """Res-9 cells whose centre lies in the shape; a shape too small to hold a centre gets the cell at its centroid."""
    try:
        cells = list(h3.geo_to_cells(geom, RES))
    except Exception:                    # bad or missing geometry: fall back to the centroid, if any
        cells = []
    if not cells and (c := gf._centroid(geom or {})):
        cells = [h3.latlng_to_cell(c[0], c[1], RES)]
    return cells


def build(pages, pv: str) -> tuple[list[dict], list[dict], dict]:
    """Pages of one province → (cell rows, tambon rows, counts). Reads one page at a time."""
    cells: dict[str, list] = {}          # h3 → [years, ap_code, tb_code]; the first shape's codes win
    tambons: dict[str, dict] = {}
    n = {"shapes": 0, "no_years": 0, "no_tb_code": 0, "freq_mismatch": 0}
    for page in pages:
        for f in page["features"]:
            n["shapes"] += 1
            s = parse_shape(f)
            if not s["years"]:
                n["no_years"] += 1
                continue
            if s["freq_gistda"] is not None and int(s["freq_gistda"]) != s["years"].bit_count():
                n["freq_mismatch"] += 1
            shape_cells = cells_of(s["geometry"])
            for c in shape_cells:
                if c in cells:
                    cells[c][0] |= s["years"]
                else:
                    cells[c] = [s["years"], s["ap_code"], s["tb_code"]]
            if not (tb := s["tb_code"]):
                n["no_tb_code"] += 1
                continue
            t = tambons.setdefault(tb, {"tb_code": tb, "ap_code": s["ap_code"], "pv_code": s["pv_code"] or pv,
                                        "tambon": s["tambon"], "amphoe": s["amphoe"], "province": s["province"],
                                        "years": 0, "shapes": 0, "rai_by_year": {}})
            t["years"] |= s["years"]
            t["shapes"] += 1
            rai = s["area_rai"] if s["area_rai"] is not None else \
                sum(h3.cell_area(c, "km^2") for c in shape_cells) * RAI_PER_KM2
            for y in years_of(s["years"]):
                t["rai_by_year"][str(y)] = t["rai_by_year"].get(str(y), 0.0) + rai
    cell_rows = []
    for c, (years, ap, tb) in cells.items():
        lat, lon = h3.cell_to_latlng(c)
        cell_rows.append({"pv_code": pv, "h3": c, "lat": lat, "lon": lon, "years": years, "freq": years.bit_count(),
                          "ap_code": ap, "tb_code": tb})
    for t in tambons.values():
        t["freq"] = t["years"].bit_count()
        t["rai_by_year"] = {y: round(r, 1) for y, r in sorted(t["rai_by_year"].items())}
    return cell_rows, list(tambons.values()), n


# --- fetching ----------------------------------------------------------------------------------------

def _client(key: str) -> httpx.Client:
    return httpx.Client(timeout=httpx.Timeout(120, connect=20),
                        headers={"API-Key": key, "User-Agent": "NamMa flood assistant (hackathon)"})


def fetch_page(client: httpx.Client, pv: str, offset: int, attempts: int = 3) -> dict:
    params = {"pv_idn": pv, "limit": PAGE, "offset": offset}
    failures = limited = 0
    while True:
        try:
            r = client.get(BASE, params=params)
        except httpx.TransportError:
            failures += 1
            if failures >= attempts:
                raise
            time.sleep(10 * failures)
            continue
        if r.status_code == 429:
            limited += 1
            if limited >= RATE_LIMIT_TRIES:
                raise GistdaError(f"pv{pv} offset {offset}: still rate-limited after {limited} tries")
            wait = r.headers.get("Retry-After", "")
            time.sleep(int(wait) if wait.isdigit() else RATE_LIMIT_WAIT_S)
            continue
        if r.status_code in (401, 403, 407):
            raise GistdaError(f"GISTDA refused the key ({r.status_code}): {r.text[:200]}")
        if r.status_code >= 500:
            failures += 1
            if failures < attempts:
                time.sleep(10 * failures)
                continue
        r.raise_for_status()
        body = r.json()
        if not isinstance(body, dict) or not isinstance(body.get("features"), list):
            raise GistdaError(f"not a FeatureCollection: {str(body)[:200]}")
        return body


def _path(d: Path, offset: int) -> Path:
    return d / f"{offset:07d}.json.gz"


def _load(d: Path, offset: int) -> dict | None:
    p = _path(d, offset)
    if not p.exists():
        return None
    with gzip.open(p, "rt", encoding="utf-8") as f:
        return json.load(f)


def _save(d: Path, offset: int, page: dict, key: str | None) -> int:
    """Gzipped, without `links` (they echo the request URL) and with the key removed. Returns bytes written."""
    body = gf._scrub(json.dumps({k: v for k, v in page.items() if k != "links"}, ensure_ascii=False), key)
    tmp = _path(d, offset).with_suffix(".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        f.write(body)
    tmp.replace(_path(d, offset))        # a page file on disk is always whole
    return _path(d, offset).stat().st_size


def _check_filter(page: dict, pv: str) -> None:
    """GISTDA may ignore a filter it doesn't know and send the whole country: refuse a page with another province."""
    for f in page["features"]:
        got = parse_shape(f)["pv_code"]
        if got and got.zfill(2) != pv.zfill(2):
            raise GistdaError(f"asked for pv_idn={pv}, got a shape in province {got}: the filter looks ignored")


def download(pv: str, d: Path, key: str | None, client: httpx.Client | None = None) -> dict:
    """Every page of one province onto disk, skipping pages already there. Ends at numberMatched when the first page
    gives one, else at a short page. Pages after the first are fetched WORKERS at a time, request starts at least
    MIN_INTERVAL_S apart. Raises on a province that ends early, so a partial copy is never built."""
    d.mkdir(parents=True, exist_ok=True)
    own = client is None
    started = time.monotonic()
    lock = threading.Lock()
    next_start = [0.0]                   # monotonic time the next request may start
    fetched = [0]

    def ensure_client() -> httpx.Client:
        nonlocal client
        with lock:
            if client is None:
                if not key:
                    raise GistdaError("GISTDA_API_KEY is not set and pages are missing from " + str(d))
                client = _client(key)
            return client

    def pace() -> None:
        with lock:
            now = time.monotonic()
            at = max(now, next_start[0])
            next_start[0] = at + MIN_INTERVAL_S
        if at > now:
            time.sleep(at - now)

    def get(offset: int) -> dict:
        if (page := _load(d, offset)) is not None:
            return page
        c = ensure_client()
        pace()
        page = fetch_page(c, pv, offset)
        _check_filter(page, pv)
        _save(d, offset, page, key)
        with lock:
            fetched[0] += 1
        return page

    def fetch_info(offset: int) -> tuple[int, object]:
        """Shape count and head of one page: the page itself is not kept, so a province never sits in memory."""
        page = get(offset)
        return len(page["features"]), (gf._head(page) if page["features"] else None)

    try:
        first = get(0)
        total = first.get("numberMatched") if isinstance(first.get("numberMatched"), int) else None
        head = gf._head(first) if first["features"] else None
        if total is None:
            pages = 1
            offset, page = len(first["features"]), first
            while page["features"] and len(page["features"]) == PAGE:
                if offset >= MAX_SHAPES:
                    raise GistdaError(f"pv{pv}: more than {MAX_SHAPES} shapes, offset looks ignored")
                page = get(offset)
                pages += 1
                offset += len(page["features"])
        else:
            if total > MAX_SHAPES:
                raise GistdaError(f"pv{pv}: {total} shapes, more than {MAX_SHAPES}: offset looks ignored")
            if total and not first["features"]:
                raise GistdaError(f"pv{pv}: ended at 0 of its {total} shapes")
            step = len(first["features"]) or PAGE
            offsets = list(range(step, total, step))
            ex = ThreadPoolExecutor(max_workers=WORKERS)
            try:
                futures = [ex.submit(fetch_info, o) for o in offsets]
                pages = 1 + len(offsets)
                for o, f in zip(offsets, futures):
                    n, got_head = f.result()
                    expect = min(step, total - o)
                    if n == 0:
                        raise GistdaError(f"pv{pv}: ended at {o} of its {total} shapes")
                    if n != expect:
                        raise GistdaError(f"pv{pv}: page at offset {o} has {n} shapes, expected {expect}")
                    if head and got_head == head:
                        raise GistdaError(f"pv{pv}: page at offset {o} repeats the first page, offset looks ignored")
            finally:
                ex.shutdown(wait=True, cancel_futures=True)
    finally:
        if own and client is not None:
            client.close()
    return {"number_matched": total, "pages": pages, "fetched": fetched[0],
            "download_s": round(time.monotonic() - started, 1) if fetched[0] else None,
            "raw_bytes": sum(p.stat().st_size for p in d.glob("*.json.gz"))}


def read_pages(d: Path):
    """The province's pages in offset order, one at a time."""
    for p in sorted(d.glob("*.json.gz")):
        with gzip.open(p, "rt", encoding="utf-8") as f:
            yield json.load(f)


# --- OBS ---------------------------------------------------------------------------------------------

def obs_client():
    """(ObsClient, bucket) with the VM agency's temporary credentials, or (None, None) when OBS_BUCKET is not set."""
    bucket = os.environ.get("OBS_BUCKET")
    if not bucket:
        return None, None
    import certifi
    from obs import ObsClient            # esdk-obs-python; only needed where OBS_BUCKET is set
    # The SDK skips certificate checks by default, and ssl_verify=True alone loads no CA, so give it a bundle.
    return ObsClient(server=os.environ.get("OBS_ENDPOINT") or OBS_DEFAULT_ENDPOINT, ssl_verify=certifi.where(),
                     security_provider_policy="ECS"), bucket


def _ok(resp, what: str):
    if resp.status >= 300:
        raise GistdaError(f"OBS {what}: {resp.status} {resp.errorCode} {resp.errorMessage}")
    return resp.body


def upload(pv: str, d: Path, client, bucket: str) -> int:
    """The province's pages that the bucket lacks, or holds at another size (after --refetch). Returns how many."""
    prefix = f"{OBS_PREFIX}/pv{pv}/"
    have: dict[str, int] = {}
    marker = None
    while True:
        body = _ok(client.listObjects(bucket, prefix=prefix, marker=marker, max_keys=1000), "list " + prefix)
        contents = body.contents or []
        have.update((c.key, c.size) for c in contents)
        if not body.is_truncated or not contents:
            break
        marker = body.next_marker or contents[-1].key
    sent = 0
    for p in sorted(d.glob("*.json.gz")):
        key = prefix + p.name
        if have.get(key) != p.stat().st_size:
            _ok(client.putFile(bucket, key, str(p)), "put " + key)
            sent += 1
    return sent


# --- storing -----------------------------------------------------------------------------------------

CELL_COLS = ("pv_code", "h3", "lat", "lon", "years", "freq", "ap_code", "tb_code")


def store(conn, pv: str, cells: list[dict], tambons: list[dict], run: dict) -> None:
    """Replace the province's rows and record the run. The caller commits."""
    conn.execute("DELETE FROM flood_history_cells WHERE pv_code = %s", (pv,))
    conn.execute("DELETE FROM flood_history_tambon WHERE pv_code = %s", (pv,))
    with conn.cursor() as cur:
        with cur.copy(f"COPY flood_history_cells ({', '.join(CELL_COLS)}) FROM STDIN") as copy:
            for r in cells:
                copy.write_row([r[c] for c in CELL_COLS])
        cur.executemany(
            "INSERT INTO flood_history_tambon (tb_code, ap_code, pv_code, tambon, amphoe, province, years, freq, "
            "shapes, rai_by_year) VALUES (%(tb_code)s, %(ap_code)s, %(pv_code)s, %(tambon)s, %(amphoe)s, "
            "%(province)s, %(years)s, %(freq)s, %(shapes)s, %(rai)s) ON CONFLICT (tb_code) DO UPDATE SET "
            "ap_code = EXCLUDED.ap_code, pv_code = EXCLUDED.pv_code, tambon = EXCLUDED.tambon, "
            "amphoe = EXCLUDED.amphoe, province = EXCLUDED.province, years = EXCLUDED.years, freq = EXCLUDED.freq, "
            "shapes = EXCLUDED.shapes, rai_by_year = EXCLUDED.rai_by_year",
            [{**t, "rai": Jsonb(t["rai_by_year"])} for t in tambons])
    conn.execute(
        "INSERT INTO flood_history_runs (pv_code, number_matched, shapes, pages, raw_bytes, download_s, cells, "
        "tambons) VALUES (%(pv)s, %(number_matched)s, %(shapes)s, %(pages)s, %(raw_bytes)s, %(download_s)s, "
        "%(cells)s, %(tambons)s) ON CONFLICT (pv_code) DO UPDATE SET number_matched = EXCLUDED.number_matched, "
        "shapes = EXCLUDED.shapes, pages = EXCLUDED.pages, raw_bytes = EXCLUDED.raw_bytes, "
        "download_s = COALESCE(EXCLUDED.download_s, flood_history_runs.download_s), cells = EXCLUDED.cells, "
        "tambons = EXCLUDED.tambons, built_at = now()",
        {"pv": pv, **run})


def run_province(conn, pv: str, key: str | None = None, client: httpx.Client | None = None,
                 refetch: bool = False, obs=None) -> dict:
    """Download (or resume) one province, build its cells and sub-districts, store them, then copy its pages to OBS
    when `obs` is an (ObsClient, bucket) pair. Returns the run summary. A download or build failure raises and
    leaves the stored rows as they were; an upload failure raises after the rows are stored, and a rerun sends
    only what is missing."""
    d = RAW_DIR / "gistda" / "flood-freq" / f"pv{pv}"
    if refetch and d.exists():
        shutil.rmtree(d)
    got = download(pv, d, key or os.environ.get("GISTDA_API_KEY"), client)
    cells, tambons, n = build(read_pages(d), pv)
    if got["number_matched"] is not None and n["shapes"] != got["number_matched"]:
        raise GistdaError(f"pv{pv}: read {n['shapes']} shapes from disk, GISTDA counts {got['number_matched']}")
    run = {**got, **n, "cells": len(cells), "tambons": len(tambons)}
    store(conn, pv, cells, tambons, run)
    conn.commit()
    run["uploaded"] = upload(pv, d, *obs) if obs and obs[0] is not None else None
    return run


def describe(pv: str, r: dict) -> str:
    dl = f"downloaded {r['fetched']} pages in {r['download_s']:.0f} s" if r["download_s"] else "all pages on disk"
    return (f"gistda history pv{pv}: {r['shapes']:,} shapes of {r['number_matched'] or '?'} in {r['pages']} pages, "
            f"{r['raw_bytes'] / 1e6:.1f} MB gzipped ({dl}); {r['cells']:,} cells, {r['tambons']} sub-districts. "
            f"Shapes with no year: {r['no_years']}, no sub-district code: {r['no_tb_code']}, "
            f"GISTDA freq ≠ our count: {r['freq_mismatch']}. "
            + ("OBS: not set" if r.get("uploaded") is None else f"OBS: {r['uploaded']} pages sent"))


def show(conn, name: str) -> list[str]:
    rows = conn.execute("SELECT tambon, amphoe, province, years FROM flood_history_tambon WHERE tambon = %s "
                        "ORDER BY tb_code", (name,)).fetchall()
    return [f"ต.{t} อ.{a} จ.{p}: {', '.join(map(str, years_of(y)))} ({y.bit_count()} ปี)" for t, a, p, y in rows] \
        or [f"no sub-district named {name}"]


def main() -> None:
    from common.db import connect
    p = argparse.ArgumentParser()
    p.add_argument("--province", nargs="+", required=True, help="GISTDA pv_idn codes, e.g. 14 for Ayutthaya")
    p.add_argument("--refetch", action="store_true", help="delete the pages on disk and download again")
    p.add_argument("--show", help="a sub-district name to print the flooded years of, e.g. บางบาล")
    args = p.parse_args()
    key = os.environ.get("GISTDA_API_KEY")
    obs = obs_client()
    with connect() as conn:
        for pv in args.province:
            try:
                print(describe(pv, run_province(conn, pv, key, refetch=args.refetch, obs=obs)), flush=True)
            except Exception as e:
                conn.rollback()
                print(f"gistda history pv{pv} failed: {gf._scrub(repr(e), key)}", flush=True)
        if args.show:
            print("\n".join(show(conn, args.show)), flush=True)


if __name__ == "__main__":
    main()
