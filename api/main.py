"""The one always-warm app (CLAUDE.md §3): LINE webhook, web API, responder dashboard, scheduler.

    uvicorn api.main:app --port 8000        # SCHEDULER=1 also runs ingest → risk → alerts every 10 min
"""
import hmac
import os
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.staticfiles import StaticFiles
from psycopg.rows import dict_row
from pydantic import BaseModel  # noqa: E402

from agent import alerts, jobs, risk
from common.config import ROOT
from common.db import connect
from line.webhook import router as line_router
from api import auth, chat_web, stations, tickets
from api.common import install_error_handlers

DEMO = os.environ.get("DEMO_MODE", "1") == "1"


def responder(authorization: str | None = Header(None)) -> None:
    """Rescue tickets are PDPA-sensitive: responder endpoints need RESPONDER_TOKEN as a Bearer token.
    Without a token configured, only DEMO_MODE (local dev) is let through."""
    token = os.environ.get("RESPONDER_TOKEN")
    if not token:
        if DEMO:
            return
        raise HTTPException(503, "RESPONDER_TOKEN not set")
    if not authorization or not hmac.compare_digest(authorization, f"Bearer {token}"):
        raise HTTPException(401, "responder token required")



@asynccontextmanager
async def lifespan(_app):
    if os.environ.get("SCHEDULER") == "1":
        threading.Thread(target=jobs.loop, args=(int(os.environ.get("SCHEDULER_EVERY", "600")),), daemon=True).start()
    yield


app = FastAPI(title="NamMa", lifespan=lifespan)
install_error_handlers(app)
app.include_router(line_router)
# Web API contract: docs/api.md (+ src/api/types.ts in namma-web).
for module in (auth, stations, tickets, chat_web):
    app.include_router(module.router)


def _rows(sql: str, params=()) -> list[dict]:
    with connect() as conn, conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(sql, params).fetchall()


@app.get("/healthz")
def healthz():
    """503 when ingest has stalled (no risk refresh in 30 min), so an uptime check can page someone."""
    row = _rows("SELECT max(ts) AS last_reading, (SELECT max(computed_at) FROM station_risk) AS last_risk "
                "FROM waterlevel_readings")[0]
    fresh = row["last_risk"] is not None and datetime.now().astimezone() - row["last_risk"] < timedelta(minutes=30)
    if not fresh:
        raise HTTPException(503, {"ok": False, **{k: str(v) for k, v in row.items()}})
    return {"ok": True, **row}


class Simulate(BaseModel):
    station_id: int
    tier: str = "danger"


@app.post("/api/demo/simulate", dependencies=[Depends(responder)])
def api_simulate(body: Simulate):
    """Demo step 3: force a tier jump at one station and run the alert job right away."""
    if not DEMO:
        raise HTTPException(403, "DEMO_MODE off")
    if body.tier not in risk.TIERS:
        raise HTTPException(400, "bad tier")
    with connect() as conn:
        n = conn.execute("UPDATE station_risk SET risk_tier = %s, computed_at = now(), forecast_json = forecast_json || "
                         "jsonb_build_object('simulated', true) WHERE station_id = %s", (body.tier, body.station_id)).rowcount
        if not n:
            raise HTTPException(404, "station has no risk row")
        conn.commit()
        sent = alerts.run(conn)
    return {"ok": True, "alerts_sent": sent}


@app.post("/api/demo/refresh", dependencies=[Depends(responder)])
def api_refresh():
    """Recompute real risk (undoes simulate)."""
    with connect() as conn:
        res = risk.compute_all(conn)
    return {"stations": len(res)}


# The built web app (namma-web repo: npm run build) on the same origin as /api: one cert, no CORS.
# Default: the namma-web checkout next to this repo.
WEB_DIST = Path(os.environ.get("WEB_DIST", ROOT.parent / "namma-web" / "dist"))
if WEB_DIST.is_dir():
    app.mount("/", StaticFiles(directory=WEB_DIST, html=True), name="web")
