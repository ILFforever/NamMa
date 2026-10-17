"""Build data/gazetteer.csv: (province, amphoe, tambon) → centroid lat/lon of the ThaiWater
stations in that area. Rain + water level stations together cover ~740 districts, enough to
place "บ้านแม่อยู่บางบาล" on a map without a paid geocoder.

    python -m ingest.gazetteer            # uses the newest raw archive under var/raw
"""
import csv
import gzip
import json
from collections import defaultdict

from common.config import RAW_DIR, ROOT
from ingest import thaiwater

OUT = ROOT / "data" / "gazetteer.csv"


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


def build() -> list[dict]:
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
                     "lon": round(sum(b for _, b in ll) / len(ll), 5), "n": len(ll)})
    return rows


def main() -> None:
    rows = build()
    with open(OUT, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["province", "amphoe", "tambon", "lat", "lon", "n"])
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {len(rows)} places to {OUT}")


if __name__ == "__main__":
    main()
