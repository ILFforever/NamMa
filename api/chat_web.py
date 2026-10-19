"""POST /api/chat (docs/api.md → Chat): the web face of agent.chat.handle, the same agent LINE uses.

Postbacks are translated to the text the agent already understands, so the rules live in one place.
"""
import threading
import time
from collections import deque

import psycopg
from fastapi import APIRouter, Depends
from psycopg.rows import tuple_row
from pydantic import BaseModel, Field

from agent import chat, triage
from api.auth import current_user
from api.common import ApiError, envelope, get_db
from line.webhook import FALLBACK

router = APIRouter()

RATE_LIMIT, RATE_WINDOW_S = 20, 60.0
_hits: dict[str, deque] = {}          # session_id → monotonic times of recent requests (per process)
_hits_lock = threading.Lock()

# Web postback data → the agent's text command for the same action.
_POSTBACK_TEXT = {
    "consent:sensitive_memory:yes": chat.CONSENT_YES,
    "consent:sensitive_memory:no": chat.CONSENT_NO,
    "memory:delete_all": chat.FORGET_ALL_TEXT,
}
_TEXT_POSTBACK = {text: data for data, text in _POSTBACK_TEXT.items()}


class LatLon(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


class ChatIn(BaseModel):
    session_id: str = Field(min_length=1, max_length=128)
    message: str | None = Field(default=None, max_length=5000)
    postback: str | None = Field(default=None, max_length=100)
    location: LatLon | None = None


def _rate_limit(session_id: str) -> None:
    """20 requests per minute per session. In memory: one API process serves the web app."""
    now = time.monotonic()
    with _hits_lock:
        if len(_hits) > 10_000:       # drop idle sessions so the map cannot grow without bound
            for k in [k for k, q in _hits.items() if not q or now - q[-1] > RATE_WINDOW_S]:
                del _hits[k]
        q = _hits.setdefault(session_id, deque())
        while q and now - q[0] > RATE_WINDOW_S:
            q.popleft()
        if len(q) >= RATE_LIMIT:
            raise ApiError(429, "rate_limited", "too many messages; wait a minute and try again")
        q.append(now)


def _quick_out(q: chat.QuickReply) -> dict:
    label = q.label[:20]           # LINE limit; the agent already truncates, this is a guard
    if q.kind == "location":
        return {"type": "location", "label": label}
    data = _TEXT_POSTBACK.get(q.text or "")
    if data is not None:
        return {"type": "postback", "label": label, "data": data, "display_text": label}
    return {"type": "message", "label": label, "text": q.text or label}


@router.post("/api/chat")
def chat_turn(body: ChatIn, user: dict | None = Depends(current_user), conn: psycopg.Connection = Depends(get_db)):
    given = [x for x in (body.message, body.postback, body.location) if x is not None]
    if len(given) != 1:
        raise ApiError(400, "bad_request", "send exactly one of message, postback or location")
    if body.message is not None and not body.message.strip():
        raise ApiError(400, "bad_request", "message is empty")
    text = None
    if body.postback is not None:
        text = _POSTBACK_TEXT.get(body.postback)
        if text is None:
            raise ApiError(400, "bad_request", f"unknown postback: {body.postback}")
    elif body.message is not None:
        text = body.message

    _rate_limit(body.session_id)
    # Signed-in users chat as their account (LINE or Google); anonymous web chat is keyed by session.
    user_id = user["id"] if user else None
    external_id = None if user else f"web:{body.session_id}"
    conn.row_factory = tuple_row   # agent code reads rows by position (row[0])
    try:
        if body.location is not None:
            reply = chat.handle(conn, external_id, "web", location=(body.location.lat, body.location.lon), user_id=user_id)
        else:
            reply = chat.handle(conn, external_id, "web", text=text, user_id=user_id)
    except Exception as e:
        # Same promise as LINE: never leave someone who may be in danger with no answer.
        conn.rollback()
        conn.execute("SET TIME ZONE 'Asia/Bangkok'")
        print(f"chat failed for web session: {e!r}", flush=True)
        reply = chat.Reply(FALLBACK, emergency=triage.is_danger(body.message or ""))

    return envelope(conn, {
        "reply": reply.text,
        "sources": reply.sources,
        "quick_replies": [_quick_out(q) for q in reply.quick],
        "emergency": reply.emergency,
    })
