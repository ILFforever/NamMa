"""Web login (docs/api.md → Auth): LINE ID token → session token; responder role for the dashboard.

Web and LINE share one users row (same LINE userId), so a responder's LINE account is their login.
"""
import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone

import httpx
import psycopg
from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel

from api.common import ApiError, envelope, get_db

SESSION_DAYS = 7
DEMO = os.environ.get("DEMO_MODE", "1") == "1"

router = APIRouter()


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(conn: psycopg.Connection, user_id: int) -> str:
    """New bearer token for this user. Only its hash is stored."""
    token = secrets.token_urlsafe(32)
    conn.execute("INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (%s, %s, %s)",
                 (_hash(token), user_id, datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)))
    return token


def current_user(authorization: str | None = Header(None), conn: psycopg.Connection = Depends(get_db)) -> dict | None:
    """The signed-in user, or None with no Authorization header. A bad or expired token is 401 invalid_token."""
    if not authorization:
        return None
    if not authorization.startswith("Bearer "):
        raise ApiError(401, "invalid_token", "expected 'Authorization: Bearer <token>'")
    row = conn.execute(
        "SELECT u.id, u.line_user_id, u.display_name, u.role, u.picture_url FROM sessions s "
        "JOIN users u ON u.id = s.user_id WHERE s.token_hash = %s AND s.expires_at > now()",
        (_hash(authorization.removeprefix("Bearer ").strip()),)).fetchone()
    if not row:
        raise ApiError(401, "invalid_token", "session expired or unknown")
    return row


def require_user(user: dict | None = Depends(current_user)) -> dict:
    if user is None:
        raise ApiError(401, "unauthorized", "sign in first")
    return user


def require_responder(user: dict = Depends(require_user)) -> dict:
    """Rescue tickets are PDPA-sensitive: responders only. The static bundle is public, so this is the guard."""
    if user["role"] != "responder":
        raise ApiError(403, "forbidden", "responder role required")
    return user


def _responder_allowlist() -> set[str]:
    return {x.strip() for x in os.environ.get("RESPONDER_LINE_IDS", "").split(",") if x.strip()}


def _verify_line(id_token: str) -> dict:
    """Verify a LIFF ID token with LINE. Returns {sub, name, picture}."""
    channel = os.environ.get("LINE_LOGIN_CHANNEL_ID")
    if not channel:
        raise ApiError(503, "unavailable", "LINE_LOGIN_CHANNEL_ID not set")
    try:
        r = httpx.post("https://api.line.me/oauth2/v2.1/verify",
                       data={"id_token": id_token, "client_id": channel}, timeout=10)
    except httpx.HTTPError:
        raise ApiError(503, "unavailable", "could not reach LINE to verify the token")
    if r.status_code != 200:
        raise ApiError(401, "invalid_token", "LINE rejected the ID token")
    return r.json()


def _user_out(u: dict) -> dict:
    return {"display_name": u["display_name"], "role": u["role"], "picture_url": u["picture_url"]}


class LoginIn(BaseModel):
    id_token: str


@router.post("/api/auth/line")
def login(body: LoginIn, conn: psycopg.Connection = Depends(get_db)):
    if body.id_token.startswith("demo:"):
        # Local/demo only: "demo:user" or "demo:responder" (the web app's mock mode uses the same ids).
        role = body.id_token.removeprefix("demo:")
        if not DEMO or role not in ("user", "responder"):
            raise ApiError(401, "invalid_token", "demo login is off")
        line_id, name, picture = f"demo:{role}", "เจ้าหน้าที่ทดลอง" if role == "responder" else "ผู้ใช้ทดลอง", None
    else:
        claims = _verify_line(body.id_token)
        line_id, name, picture, role = claims["sub"], claims.get("name"), claims.get("picture"), None
        if line_id in _responder_allowlist():
            role = "responder"

    u = conn.execute(
        "INSERT INTO users (line_user_id, display_name, picture_url, role) VALUES (%s, %s, %s, coalesce(%s, 'user')) "
        "ON CONFLICT (line_user_id) DO UPDATE SET display_name = coalesce(EXCLUDED.display_name, users.display_name), "
        "picture_url = coalesce(EXCLUDED.picture_url, users.picture_url), "
        "role = CASE WHEN %s::text IS NULL THEN users.role ELSE %s::text END "
        "RETURNING id, display_name, role, picture_url",
        (line_id, name, picture, role, role, role)).fetchone()
    return envelope(conn, {"session_token": create_session(conn, u["id"]), "user": _user_out(u)})


@router.get("/api/me")
def me(user: dict = Depends(require_user), conn: psycopg.Connection = Depends(get_db)):
    return envelope(conn, _user_out(user))
