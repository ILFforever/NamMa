"""The 10-minute pipeline: ThaiWater ingest → station_risk → alerts.

    python -m agent.jobs              # once
    python -m agent.jobs --every 600  # loop (also started inside the API when SCHEDULER=1)
"""
import argparse
import os
import shutil
import time
from datetime import date, datetime, timedelta

from agent import alerts, risk
from common.config import RAW_DIR
from common.db import connect
from ingest import run as ingest

RAW_KEEP_DAYS = int(os.environ.get("RAW_KEEP_DAYS", "7"))   # ~0.5 MB per run, ~75 MB/day; OBS keeps the long-term copy
KEEP_MESSAGES = 200                                          # per user; the agent only reads the last 10


def cleanup(conn) -> None:
    cutoff = date.today() - timedelta(days=RAW_KEEP_DAYS)
    for d in RAW_DIR.glob("20??-??-??"):
        if d.is_dir() and date.fromisoformat(d.name) < cutoff:
            shutil.rmtree(d, ignore_errors=True)
    conn.execute("DELETE FROM messages WHERE id IN (SELECT id FROM (SELECT id, row_number() OVER "
                 "(PARTITION BY user_id ORDER BY ts DESC) AS rn FROM messages) x WHERE rn > %s)", (KEEP_MESSAGES,))


def run_once() -> None:
    ingest.run_once()
    with connect() as conn:
        results = risk.compute_all(conn)
        try:
            from model import forecast    # optional: needs lightgbm + model/artifacts
            forecast.score(conn)
        except Exception as e:
            print(f"forecast scoring skipped: {e!r}", flush=True)
        cleanup(conn)
        conn.commit()
        sent = alerts.run(conn)
    counts = {t: sum(r.risk_tier == t for r in results) for t in risk.TIERS}
    print(f"{datetime.now():%H:%M} risk {counts} alerts_sent={len(sent)}", flush=True)


def loop(every: int) -> None:
    while True:
        try:
            run_once()
        except Exception as e:  # keep looping through API hiccups
            print(f"jobs failed: {e!r}", flush=True)
        time.sleep(every)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--every", type=int, help="seconds between runs; omit to run once")
    args = p.parse_args()
    loop(args.every) if args.every else run_once()


if __name__ == "__main__":
    main()
