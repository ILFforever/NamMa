"""Build data/gazetteer.csv: (province, amphoe, tambon) → lat/lon, so "บ้านอยู่ลุมพินี กรุงเทพ" lands on a map
without a paid geocoder.

Base: every province, district and subdistrict (77 / 928 / 7,425) with its official Thai name and polygon centroid,
from the Royal Thai Survey Department's admin boundaries as published by OCHA on HDX (COD-AB "cod-ab-tha",
v01 2022-01-22, CC BY-IGO: credit RTSD / OCHA). Added on top: names ThaiWater's station geocodes use that RTSD
spells differently (district "บึงกาฬ" for RTSD's "เมืองบึงกาฬ"), placed at the centroid of their stations.

    python -m ingest.gazetteer            # downloads the COD-AB table (~2 MB, needs openpyxl), newest raw ThaiWater archive
    python -m ingest.gazetteer --codab tha_admin_boundaries.xlsx
"""
import argparse
import csv
import gzip
import json
import tempfile
from collections import defaultdict
from pathlib import Path

import httpx

from common.config import RAW_DIR, ROOT
from ingest import thaiwater

OUT = ROOT / "data" / "gazetteer.csv"
CODAB_URL = ("https://data.humdata.org/dataset/d24bdc45-eb4c-4e3d-8b16-44db02667c27/resource/"
             "a925b917-01ae-48c4-9279-7efe680a6b11/download/tha_admin_boundaries.xlsx")
FIELDS = ["province", "amphoe", "tambon", "lat", "lon", "src"]


def _newest(name: str):
    files = sorted(RAW_DIR.glob(f"*/{name}_*.json.gz"))
    if files:
        with gzip.open(files[-1], "rt", encoding="utf-8") as f:
            return json.load(f)
    return {"waterlevel_load": thaiwater.fetch_waterlevel_load, "rain_24h": thaiwater.fetch_rain_24h}[name]()


def _points(records):
    for r in records:
        s, g = r.get("station") or {}, r.get("geocode") or {}
        lat, lon = thaiwater._num(s.get("tele_station_lat")), thaiwater._num(s.get("tele_station_long"))
        prov, amp, tam = (thaiwater._th(g, k) for k in ("province_name", "amphoe_name", "tumbon_name"))
        if lat and lon and prov:
            yield prov, amp or "", tam or "", lat, lon


def official(xlsx: Path) -> list[dict]:
    """Every admin unit in the COD-AB table: Thai names (name1) and the polygon centroid."""
    import openpyxl     # only needed to rebuild the CSV, not on the server
    wb = openpyxl.load_workbook(xlsx, read_only=True)
    rows = []
    for level in (1, 2, 3):
        it = wb[f"tha_admin{level}"].iter_rows(values_only=True)
        head = next(it)
        for r in it:
            r = dict(zip(head, r))
            names = [r.get(f"adm{i}_name1") or "" for i in (1, 2, 3)][:level] + ["", ""]
            rows.append({"province": names[0], "amphoe": names[1], "tambon": names[2],
                         "lat": round(r["center_lat"], 5), "lon": round(r["center_lon"], 5), "src": "rtsd"})
    return rows


def from_stations() -> list[dict]:
    pts = list(_points(_newest("waterlevel_load")["waterlevel_data"]["data"])) + list(_points(_newest("rain_24h")["data"]))
    groups = defaultdict(list)
    for prov, amp, tam, lat, lon in pts:
        groups[(prov, "", "")].append((lat, lon))
        if amp:
            groups[(prov, amp, "")].append((lat, lon))
            if tam:
                groups[(prov, amp, tam)].append((lat, lon))
    rows = []
    for (prov, amp, tam), ll in sorted(groups.items()):
        rows.append({"province": prov, "amphoe": amp, "tambon": tam,
                     "lat": round(sum(a for a, _ in ll) / len(ll), 5),
                     "lon": round(sum(b for _, b in ll) / len(ll), 5), "src": "thaiwater"})
    return rows


def build(xlsx: Path) -> list[dict]:
    rows = official(xlsx)
    provinces = {r["province"] for r in rows}
    known = {(r["province"], r["amphoe"], r["tambon"]) for r in rows}
    # Station names RTSD spells differently; stations geocoded outside Thailand (a Myanmar "province") are dropped.
    extra = [r for r in from_stations() if r["province"] in provinces and (r["province"], r["amphoe"], r["tambon"]) not in known]
    return sorted(rows + extra, key=lambda r: (r["province"], r["amphoe"], r["tambon"]))


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--codab", type=Path, help="tha_admin_boundaries.xlsx from HDX cod-ab-tha (downloaded if omitted)")
    args = p.parse_args()
    xlsx = args.codab
    if not xlsx:
        xlsx = Path(tempfile.mkdtemp()) / "tha_admin_boundaries.xlsx"
        r = httpx.get(CODAB_URL, follow_redirects=True, timeout=120)
        r.raise_for_status()
        xlsx.write_bytes(r.content)
    rows = build(xlsx)
    with open(OUT, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {len(rows)} places to {OUT} ({sum(r['src'] == 'thaiwater' for r in rows)} from station geocodes only)")


if __name__ == "__main__":
    main()
