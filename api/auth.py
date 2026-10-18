"""Accounts and web login (docs/api.md → Auth): LINE or Google ID token → session token; responder role.

One account per person. It can sign in with LINE, with Google, or both linked (user_identities). Web and the LINE OA
share one users row (same LINE userId), so a responder's LINE account is their login. Signing in folds the
browser's anonymous web chat (its places and memory) into the account.
"""
import hashlib
import os
import re
import secrets
from datetime import datetime, timedelta, timezone

import httpx
import psycopg
from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, Field

from agent import tools
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


def _verify_google(id_token: str) -> dict:
    """Verify a Google Sign-In ID token. Returns its claims {sub, email, email_verified, name, picture}."""
    client = os.environ.get("GOOGLE_CLIENT_ID")
    if not client:
        raise ApiError(503, "unavailable", "GOOGLE_CLIENT_ID not set")
    try:
        r = httpx.get("https://oauth2.googleapis.com/tokeninfo", params={"id_token": id_token}, timeout=10)
    except httpx.HTTPError:
        raise ApiError(503, "unavailable", "could not reach Google to verify the token")
    claims = r.json() if r.status_code == 200 else {}
    if claims.get("aud") != client or claims.get("iss") not in ("accounts.google.com", "https://accounts.google.com"):
        raise ApiError(401, "invalid_token", "Google rejected the ID token")
    return claims


DEMO_NAME = re.compile(r"[a-z0-9_-]{1,40}")


def _line_claims(id_token: str) -> dict:
    """{sub, name, picture, role}. Demo tokens: 'demo:user', 'demo:responder', 'demo:user:<name>' (one account per
    name, so testers don't share memory)."""
    if id_token.startswith("demo:"):
        parts = id_token.split(":")
        if not DEMO or parts[1:2] not in (["user"], ["responder"]) or len(parts) > 3 or \
                (len(parts) == 3 and not DEMO_NAME.fullmatch(parts[2])):
            raise ApiError(401, "invalid_token", "demo login is off or the demo token is malformed")
        role = parts[1]
        name = parts[2] if len(parts) == 3 else ("เจ้าหน้าที่ทดลอง" if role == "responder" else "ผู้ใช้ทดลอง")
        return {"sub": id_token, "name": name, "picture": None, "role": role}
    claims = _verify_line(id_token)
    return {"sub": claims["sub"], "name": claims.get("name"), "picture": claims.get("picture"),
            "role": "responder" if claims["sub"] in _responder_allowlist() else None}


def _google_claims(id_token: str) -> dict:
    """{sub, email, name, picture}. Demo token 'demo:google:<name>' when DEMO_MODE is on."""
    if id_token.startswith("demo:"):
        parts = id_token.split(":")
        if not DEMO or len(parts) != 3 or parts[1] != "google" or not DEMO_NAME.fullmatch(parts[2]):
            raise ApiError(401, "invalid_token", "demo login is off or the demo token is malformed")
        return {"sub": id_token, "email": f"{parts[2]}@demo.namma", "name": parts[2], "picture": None}
    claims = _verify_google(id_token)
    if claims.get("email_verified") not in (True, "true"):
        raise ApiError(401, "invalid_token", "the Google account's email is not verified")
    return {"sub": claims["sub"], "email": claims.get("email"), "name": claims.get("name"),
            "picture": claims.get("picture")}


def _user_row(conn: psycopg.Connection, user_id: int) -> dict:
    return conn.execute("SELECT id, line_user_id, display_name, role, picture_url FROM users WHERE id = %s",
                        (user_id,)).fetchone()


def _user_out(conn: psycopg.Connection, u: dict) -> dict:
    google = conn.execute("SELECT email FROM user_identities WHERE user_id = %s AND provider = 'google'",
                          (u["id"],)).fetchone()
    return {"display_name": u["display_name"], "role": u["role"], "picture_url": u["picture_url"],
            "identities": {"line": tools.is_line_id(u["line_user_id"]),
                           "google": google["email"] if google else None}}


def _absorb_chat(conn: psycopg.Connection, user_id: int, chat_session: str | None) -> bool:
    """Fold the anonymous web chat the person used before signing in into their account."""
    if not chat_session:
        return False
    anon = conn.execute("SELECT id FROM users WHERE line_user_id = %s", (f"web:{chat_session}",)).fetchone()
    return bool(anon) and tools.merge_users(conn, anon["id"], user_id)


def _signed_in(conn: psycopg.Connection, user_id: int, chat_session: str | None) -> dict:
    merged = _absorb_chat(conn, user_id, chat_session)
    return envelope(conn, {"session_token": create_session(conn, user_id), "user": _user_out(conn, _user_row(conn, user_id)),
                           "merged_chat": merged})


class LoginIn(BaseModel):
    id_token: str
    # The anonymous web chat's session_id (POST /api/chat). Its places and memory join the account.
    chat_session: str | None = Field(default=None, max_length=128)


def _line_user(conn: psycopg.Connection, c: dict) -> int:
    """The account with this LINE id, created on first sign-in (LINE OA chatters already have one)."""
    return conn.execute(
        "INSERT INTO users (line_user_id, display_name, picture_url, role) VALUES (%s, %s, %s, coalesce(%s, 'user')) "
        "ON CONFLICT (line_user_id) DO UPDATE SET display_name = coalesce(EXCLUDED.display_name, users.display_name), "
        "picture_url = coalesce(EXCLUDED.picture_url, users.picture_url), "
        "role = CASE WHEN %s::text IS NULL THEN users.role ELSE %s::text END RETURNING id",
        (c["sub"], c["name"], c["picture"], c["role"], c["role"], c["role"])).fetchone()["id"]


def _google_user(conn: psycopg.Connection, c: dict) -> int:
    """The account this Google identity belongs to, created on first sign-in (no LINE id until linked)."""
    row = conn.execute("SELECT user_id FROM user_identities WHERE provider = 'google' AND subject = %s",
                       (c["sub"],)).fetchone()
    if row:
        conn.execute("UPDATE user_identities SET email = %s WHERE provider = 'google' AND subject = %s",
                     (c["email"], c["sub"]))
        return row["user_id"]
    uid = conn.execute("INSERT INTO users (display_name, picture_url) VALUES (%s, %s) RETURNING id",
                       (c["name"], c["picture"])).fetchone()["id"]
    conn.execute("INSERT INTO user_memory (user_id) VALUES (%s)", (uid,))
    conn.execute("INSERT INTO user_identities (provider, subject, user_id, email) VALUES ('google', %s, %s, %s)",
                 (c["sub"], uid, c["email"]))
    return uid


@router.post("/api/auth/line")
def login(body: LoginIn, conn: psycopg.Connection = Depends(get_db)):
    return _signed_in(conn, _line_user(conn, _line_claims(body.id_token)), body.chat_session)


@router.post("/api/auth/google")
def login_google(body: LoginIn, conn: psycopg.Connection = Depends(get_db)):
    return _signed_in(conn, _google_user(conn, _google_claims(body.id_token)), body.chat_session)


class LinkIn(BaseModel):
    id_token: str


@router.post("/api/auth/link/{provider}")
def link(provider: str, body: LinkIn, user: dict = Depends(require_user), conn: psycopg.Connection = Depends(get_db)):
    """Add a second way to sign in to the signed-in account. If that LINE or Google account already has a NamMa
    account of its own (e.g. a LINE OA chatter), the two are the same person: that account's places, memory and
    history join this one, and it is deleted. 409 if this account already has a different one of that provider."""
    if provider == "line":
        c = _line_claims(body.id_token)
        if tools.is_line_id(user["line_user_id"]) and user["line_user_id"] != c["sub"]:
            raise ApiError(409, "conflict", "this account is already linked to another LINE account")
        other = conn.execute("SELECT id FROM users WHERE line_user_id = %s", (c["sub"],)).fetchone()
        if other and other["id"] != user["id"]:
            if conn.execute("SELECT 1 FROM user_identities a JOIN user_identities b USING (provider) "
                            "WHERE a.user_id = %s AND b.user_id = %s AND a.subject <> b.subject",
                            (other["id"], user["id"])).fetchone():
                raise ApiError(409, "conflict", "both accounts have a different Google account; unlink one first")
            tools.merge_users(conn, other["id"], user["id"])
        conn.execute("UPDATE users SET line_user_id = %s, picture_url = coalesce(picture_url, %s) WHERE id = %s",
                     (c["sub"], c["picture"], user["id"]))
    elif provider == "google":
        c = _google_claims(body.id_token)
        mine = conn.execute("SELECT subject FROM user_identities WHERE user_id = %s AND provider = 'google'",
                            (user["id"],)).fetchone()
        if mine and mine["subject"] != c["sub"]:
            raise ApiError(409, "conflict", "this account is already linked to another Google account")
        other = conn.execute("SELECT user_id FROM user_identities WHERE provider = 'google' AND subject = %s",
                             (c["sub"],)).fetchone()
        if other and other["user_id"] != user["id"]:
            o = _user_row(conn, other["user_id"])
            if tools.is_line_id(o["line_user_id"]) and tools.is_line_id(user["line_user_id"]) \
                    and o["line_user_id"] != user["line_user_id"]:
                raise ApiError(409, "conflict", "both accounts have a different LINE account; unlink one first")
            tools.merge_users(conn, other["user_id"], user["id"])   # moves the Google identity too
        else:
            conn.execute("INSERT INTO user_identities (provider, subject, user_id, email) VALUES ('google', %s, %s, %s) "
                         "ON CONFLICT (provider, subject) DO UPDATE SET email = EXCLUDED.email",
                         (c["sub"], user["id"], c["email"]))
    else:
        raise ApiError(404, "not_found", f"unknown provider: {provider}")
    return envelope(conn, _user_out(conn, _user_row(conn, user["id"])))


@router.delete("/api/auth/link/{provider}")
def unlink(provider: str, user: dict = Depends(require_user), conn: psycopg.Connection = Depends(get_db)):
    """Remove a way to sign in. The account must keep at least one, or the person could never sign in again."""
    has_line = tools.is_line_id(user["line_user_id"])
    has_google = bool(conn.execute("SELECT 1 FROM user_identities WHERE user_id = %s AND provider = 'google'",
                                   (user["id"],)).fetchone())
    if provider not in ("line", "google"):
        raise ApiError(404, "not_found", f"unknown provider: {provider}")
    if (provider == "line" and not has_google) or (provider == "google" and not has_line):
        raise ApiError(409, "conflict", "this is the account's only way to sign in; delete the account instead")
    if provider == "line":
        conn.execute("UPDATE users SET line_user_id = NULL WHERE id = %s", (user["id"],))
    else:
        conn.execute("DELETE FROM user_identities WHERE user_id = %s AND provider = 'google'", (user["id"],))
    return envelope(conn, _user_out(conn, _user_row(conn, user["id"])))


@router.post("/api/auth/logout")
def logout(authorization: str | None = Header(None), user: dict = Depends(require_user),
           conn: psycopg.Connection = Depends(get_db)):
    conn.execute("DELETE FROM sessions WHERE token_hash = %s", (_hash(authorization.removeprefix("Bearer ").strip()),))
    return envelope(conn, {"signed_out": True})


@router.get("/api/me")
def me(user: dict = Depends(require_user), conn: psycopg.Connection = Depends(get_db)):
    return envelope(conn, _user_out(conn, user))


@router.delete("/api/me")
def delete_me(user: dict = Depends(require_user), conn: psycopg.Connection = Depends(get_db)):
    """PDPA erasure: the account, its sign-ins, sessions, places, memory, consents and messages. Rescue tickets
    stay for the responders' record, no longer linked to the person."""
    tools.delete_user(conn, user["id"])
    return envelope(conn, {"deleted": True})
