"""The one always-warm app (CLAUDE.md §3): LINE webhook, web API, responder dashboard, scheduler.

    uvicorn api.main:app --port 8000        # SCHEDULER=1 also runs ingest → risk → alerts every 10 min
"""
import hmac
import os
import threading
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime, timedelta

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from psycopg.rows import dict_row
from pydantic import BaseModel

from agent import alerts, chat, jobs, risk
from common.config import ROOT
from common.db import connect
from line.webhook import router as line_router

DEMO = os.environ.get("DEMO_MODE", "1") == "1"
CHAT_PER_MIN = 20
_chat_hits: dict[str, deque] = defaultdict(deque)


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


def rate_limit(key: str) -> None:
    hits, now = _chat_hits[key], time.monotonic()
    while hits and now - hits[0] > 60:
        hits.popleft()
    if len(hits) >= CHAT_PER_MIN:
        raise HTTPException(429, "too many messages, wait a minute")
    hits.append(now)


@asynccontextmanager
async def lifespan(_app):
    if os.environ.get("SCHEDULER") == "1":
        threading.Thread(target=jobs.loop, args=(int(os.environ.get("SCHEDULER_EVERY", "600")),), daemon=True).start()
    yield


app = FastAPI(title="NamMa", lifespan=lifespan)
app.include_router(line_router)


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


class ChatIn(BaseModel):
    user_key: str            # browser-generated uuid; stored as users.line_user_id = 'web:<uuid>'
    text: str | None = None
    lat: float | None = None
    lon: float | None = None


@app.post("/api/chat")
def api_chat(body: ChatIn):
    loc = (body.lat, body.lon) if body.lat is not None and body.lon is not None else None
    rate_limit(body.user_key)
    if not body.text and not loc:
        raise HTTPException(400, "text or lat/lon required")
    with connect() as conn:
        r = chat.handle(conn, f"web:{body.user_key}", "web", text=body.text, location=loc)
    return asdict(r)


@app.get("/api/messages")
def api_messages(user_key: str, after_id: int = 0):
    """Chat history incl. alerts, so the web app can show pushes without LINE."""
    return _rows("SELECT m.id, m.channel, m.role, m.content, m.ts FROM messages m JOIN users u ON u.id = m.user_id "
                 "WHERE u.line_user_id = %s AND m.id > %s ORDER BY m.id", (f"web:{user_key}", after_id))


@app.get("/api/stations")
def api_stations():
    return _rows("""
        SELECT s.id, s.oldcode, s.name_th, s.river, s.province, s.agency, s.lat, s.lon,
               r.level, r.trend_cm_per_hr, r.hours_to_bank, r.risk_tier,
               (r.forecast_json->>'diff_wl_bank')::float AS diff_wl_bank, r.forecast_json->>'reading_ts' AS reading_ts
        FROM stations s JOIN station_risk r ON r.station_id = s.id WHERE s.lat IS NOT NULL""")


@app.get("/api/stations/{station_id}/readings")
def api_readings(station_id: int, hours: int = 72):
    since = datetime.now().astimezone() - timedelta(hours=min(hours, 24 * 30))
    return _rows("SELECT ts, level_msl, diff_wl_bank, discharge FROM waterlevel_readings "
                 "WHERE station_id = %s AND ts >= %s ORDER BY ts", (station_id, since))


@app.get("/api/shelters")
def api_shelters():
    return _rows("SELECT * FROM shelters ORDER BY id")


@app.get("/api/tickets", dependencies=[Depends(responder)])
def api_tickets():
    return _rows("""
        SELECT t.*, u.display_name FROM rescue_tickets t LEFT JOIN users u ON u.id = t.user_id
        ORDER BY (t.status IN ('done', 'cancelled')), (t.urgency = 'critical') DESC, t.created_at DESC LIMIT 200""")


class TicketPatch(BaseModel):
    status: str


@app.patch("/api/tickets/{ticket_id}", dependencies=[Depends(responder)])
def api_ticket_patch(ticket_id: int, body: TicketPatch):
    if body.status not in ("unconfirmed", "open", "assigned", "done", "cancelled"):
        raise HTTPException(400, "bad status")
    with connect() as conn:
        conn.execute("UPDATE rescue_tickets SET status = %s, updated_at = now() WHERE id = %s", (body.status, ticket_id))
    return {"ok": True}


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


@app.get("/")
def index():
    return RedirectResponse("/web/index.html")


app.mount("/web", StaticFiles(directory=ROOT / "web", html=True), name="web")
