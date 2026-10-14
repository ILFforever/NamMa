"""Ingest job: ThaiWater latest water levels + rain → Postgres, raw JSON archived.

    python -m ingest.run            # once
    python -m ingest.run --every 600  # loop (local dev; on ECS use cron or systemd timer)
"""
import argparse
import gzip
import json
import time
from datetime import datetime

from common.config import RAW_DIR
from common.db import connect
from ingest import store, thaiwater


def archive(name: str, payload: dict) -> None:
    # TODO: upload to OBS when OBS_BUCKET is set.
    now = datetime.now(thaiwater.BKK)
    path = RAW_DIR / now.strftime("%Y-%m-%d") / f"{name}_{now:%H%M}.json.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)


def run_once() -> None:
    wl = thaiwater.fetch_waterlevel_load()
    archive("waterlevel_load", wl)
    stations, readings = thaiwater.parse_waterlevel_load(wl)

    rain = thaiwater.fetch_rain_24h()
    archive("rain_24h", rain)
    rain_stations, rain_readings = thaiwater.parse_rain_24h(rain)

    with connect() as conn:
        store.upsert_stations(conn, stations)
        store.upsert_readings(conn, readings)
        store.upsert_rain_stations(conn, rain_stations)
        store.upsert_rain(conn, rain_readings)
    print(f"{datetime.now(thaiwater.BKK):%Y-%m-%d %H:%M} stations={len(stations)} readings={len(readings)} "
          f"rain_stations={len(rain_stations)} rain={len(rain_readings)}", flush=True)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--every", type=int, help="seconds between runs; omit to run once")
    args = p.parse_args()
    while True:
        try:
            run_once()
        except Exception as e:  # keep looping through API hiccups
            if not args.every:
                raise
            print(f"ingest failed: {e!r}", flush=True)
        if not args.every:
            break
        time.sleep(args.every)


if __name__ == "__main__":
    main()
