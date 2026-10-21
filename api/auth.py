"""Accounts and web login (docs/api.md → Auth): LINE or Google ID token → session token.

One account per person. It can sign in with LINE, with Google, or both linked (user_identities). Web and the LINE OA
share one users row (same LINE userId), so a LINE OA chatter's places and memory are there when they sign in on the
web. Signing in folds the browser's anonymous web chat (its places and memory) into the account.
"""
import hashlib
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Literal
from urllib.parse import urlencode

import httpx
import psycopg
from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from agent import tools
from api.common import ApiError, envelope, get_db

SESSION_DAYS = 90
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
        "SELECT u.id, u.line_user_id, u.display_name, u.picture_url FROM sessions s "
        "JOIN users u ON u.id = s.user_id WHERE s.token_hash = %s AND s.expires_at > now()",
        (_hash(authorization.removeprefix("Bearer ").strip()),)).fetchone()
    if not row:
        raise ApiError(401, "invalid_token", "session expired or unknown")
    return row


def optional_user(authorization: str | None = Header(None), conn: psycopg.Connection = Depends(get_db)) -> dict | None:
    """Like current_user, but a bad or expired token is anonymous instead of 401: for chat, which must always answer."""
    try:
        return current_user(authorization, conn)
    except ApiError:
        return None


def require_user(user: dict | None = Depends(current_user)) -> dict:
    if user is None:
        raise ApiError(401, "unauthorized", "sign in first")
    return user


def _verify_line(id_token: str, nonce: str | None = None) -> dict:
    """Verify a LINE ID token (LIFF, or LINE web login with its nonce) with LINE. Returns {sub, name, picture}."""
    channel = os.environ.get("LINE_LOGIN_CHANNEL_ID")
    if not channel:
        raise ApiError(503, "unavailable", "LINE_LOGIN_CHANNEL_ID not set")
    data = {"id_token": id_token, "client_id": channel} | ({"nonce": nonce} if nonce else {})
    try:
        r = httpx.post("https://api.line.me/oauth2/v2.1/verify", data=data, timeout=10)
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
    """{sub, name, picture}. Demo tokens: 'demo:user', 'demo:user:<name>' (one account per name, so testers don't
    share memory)."""
    if id_token.startswith("demo:"):
        parts = id_token.split(":")
        if not DEMO or parts[1:2] != ["user"] or len(parts) > 3 or \
                (len(parts) == 3 and not DEMO_NAME.fullmatch(parts[2])):
            raise ApiError(401, "invalid_token", "demo login is off or the demo token is malformed")
        name = parts[2] if len(parts) == 3 else "ผู้ใช้ทดลอง"
        return {"sub": id_token, "name": name, "picture": None}
    claims = _verify_line(id_token)
    return {"sub": claims["sub"], "name": claims.get("name"), "picture": claims.get("picture")}


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
    return conn.execute("SELECT id, line_user_id, display_name, picture_url FROM users WHERE id = %s",
                        (user_id,)).fetchone()


def _user_out(conn: psycopg.Connection, u: dict) -> dict:
    google = conn.execute("SELECT email FROM user_identities WHERE user_id = %s AND provider = 'google'",
                          (u["id"],)).fetchone()
    return {"display_name": u["display_name"], "picture_url": u["picture_url"],
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
        "INSERT INTO users (line_user_id, display_name, picture_url) VALUES (%s, %s, %s) "
        "ON CONFLICT (line_user_id) DO UPDATE SET display_name = coalesce(EXCLUDED.display_name, users.display_name), "
        "picture_url = coalesce(EXCLUDED.picture_url, users.picture_url) RETURNING id",
        (c["sub"], c["name"], c["picture"])).fetchone()["id"]


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


def _link_line(conn: psycopg.Connection, user: dict, c: dict) -> None:
    """Add LINE account `c` ({sub, name, picture}) to `user`, folding in that LINE account's own NamMa account."""
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


class LinkIn(BaseModel):
    id_token: str


@router.post("/api/auth/link/{provider}")
def link(provider: str, body: LinkIn, user: dict = Depends(require_user), conn: psycopg.Connection = Depends(get_db)):
    """Add a second way to sign in to the signed-in account. If that LINE or Google account already has a NamMa
    account of its own (e.g. a LINE OA chatter), the two are the same person: that account's places, memory and
    history join this one, and it is deleted. 409 if this account already has a different one of that provider."""
    if provider == "line":
        _link_line(conn, user, _line_claims(body.id_token))
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


# LINE web login (docs/api.md): the browser goes to LINE and LINE sends it back to /api/auth/line/callback, here,
# in the same browser, so the person is signed in in Safari or Chrome. (liff.login() did this in the page and lost
# the login on phones; liff.line.me signs in inside LINE's own browser instead.) LIFF stays for inside the LINE app.
LINE_STATE_COOKIE = "namma_line_state"
LINE_COOKIE_PATH = "/api/auth/line"
LINE_LOGIN_MINUTES = 10


def _line_web_config() -> tuple[str, str]:
    channel, secret = os.environ.get("LINE_LOGIN_CHANNEL_ID"), os.environ.get("LINE_LOGIN_CHANNEL_SECRET")
    if not channel or not secret:
        raise ApiError(503, "unavailable", "LINE_LOGIN_CHANNEL_ID / LINE_LOGIN_CHANNEL_SECRET not set")
    return channel, secret


def _https(request: Request) -> bool:
    return request.headers.get("x-forwarded-proto", request.url.scheme) == "https"


def _line_callback_url(request: Request) -> str:
    """Must be registered as a Callback URL on the LINE Login channel. PUBLIC_URL, or what Caddy forwarded."""
    base = os.environ.get("PUBLIC_URL") or \
        f"{'https' if _https(request) else 'http'}://{request.headers.get('host', request.url.netloc)}"
    return base.rstrip("/") + "/api/auth/line/callback"


class LineStartIn(BaseModel):
    intent: Literal["login", "link"] = "login"
    chat_session: str | None = Field(default=None, max_length=128)


@router.post("/api/auth/line/start")
def line_start(body: LineStartIn, request: Request, response: Response, user: dict | None = Depends(current_user),
               conn: psycopg.Connection = Depends(get_db)):
    """Where to send the browser for LINE Login. Sets the state cookie that ties LINE's answer to this browser."""
    channel, _ = _line_web_config()
    if body.intent == "link" and user is None:
        raise ApiError(401, "unauthorized", "sign in first")
    state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(16)
    conn.execute("DELETE FROM line_logins WHERE created_at < now() - make_interval(mins => %s)", (LINE_LOGIN_MINUTES,))
    conn.execute("INSERT INTO line_logins (state_hash, nonce, intent, link_user_id, chat_session) "
                 "VALUES (%s, %s, %s, %s, %s)",
                 (_hash(state), nonce, body.intent, user["id"] if body.intent == "link" else None,
                  body.chat_session if body.intent == "login" else None))
    response.set_cookie(LINE_STATE_COOKIE, state, max_age=LINE_LOGIN_MINUTES * 60, path=LINE_COOKIE_PATH,
                        httponly=True, secure=_https(request), samesite="lax")
    url = "https://access.line.me/oauth2/v2.1/authorize?" + urlencode({
        "response_type": "code", "client_id": channel, "redirect_uri": _line_callback_url(request),
        # No disable_auto_login: LINE's page hands the login to the LINE app and back to this browser. Without the
        # app, LINE's web form needs an email + password on the LINE account, which many people don't have (445).
        "state": state, "scope": "openid profile", "nonce": nonce})
    return envelope(conn, {"authorize_url": url})


def _line_back(**query: str) -> RedirectResponse:
    """Back to the account page with line_claim=… or line_error=… (the page reads and removes them)."""
    r = RedirectResponse("/?" + urlencode(query) + "#/account", status_code=303)
    r.delete_cookie(LINE_STATE_COOKIE, path=LINE_COOKIE_PATH)
    return r


@router.get("/api/auth/line/callback")
def line_callback(request: Request, state: str = "", code: str = "", error: str = "",
                  conn: psycopg.Connection = Depends(get_db)):
    """LINE sends the browser here. Signs in (or links) and hands the page a one-time claim for its session.
    line_error: expired (no such trip, or too old), browser (LINE came back in another browser: the state cookie
    is missing), cancelled, conflict (link: see POST /api/auth/link), failed."""
    row = conn.execute("SELECT * FROM line_logins WHERE state_hash = %s AND claim_hash IS NULL "
                       "AND created_at > now() - make_interval(mins => %s)",
                       (_hash(state), LINE_LOGIN_MINUTES)).fetchone() if state else None
    if not row:
        return _line_back(line_error="expired")
    cookie = request.cookies.get(LINE_STATE_COOKIE)
    if not cookie or not secrets.compare_digest(cookie, state):
        return _line_back(line_error="browser")
    if error or not code:
        conn.execute("DELETE FROM line_logins WHERE state_hash = %s", (row["state_hash"],))
        return _line_back(line_error="cancelled")
    channel, secret = _line_web_config()
    try:
        r = httpx.post("https://api.line.me/oauth2/v2.1/token", timeout=10, data={
            "grant_type": "authorization_code", "code": code, "redirect_uri": _line_callback_url(request),
            "client_id": channel, "client_secret": secret})
        if r.status_code != 200 or "id_token" not in r.json():
            return _line_back(line_error="failed")
        claims = _verify_line(r.json()["id_token"], nonce=row["nonce"])
    except (httpx.HTTPError, ApiError):
        return _line_back(line_error="failed")
    c = {"sub": claims["sub"], "name": claims.get("name"), "picture": claims.get("picture")}
    merged = False
    if row["intent"] == "link":
        user_id = row["link_user_id"]
        try:
            _link_line(conn, _user_row(conn, user_id), c)
        except ApiError:
            return _line_back(line_error="conflict")
    else:
        user_id = _line_user(conn, c)
        merged = _absorb_chat(conn, user_id, row["chat_session"])
    claim = secrets.token_urlsafe(32)
    conn.execute("UPDATE line_logins SET claim_hash = %s, user_id = %s, merged_chat = %s WHERE state_hash = %s",
                 (_hash(claim), user_id, merged, row["state_hash"]))
    return _line_back(line_claim=claim)


class LineClaimIn(BaseModel):
    claim: str = Field(max_length=128)


@router.post("/api/auth/line/claim")
def line_claim(body: LineClaimIn, conn: psycopg.Connection = Depends(get_db)):
    """The session for a finished LINE web login, once. 401 invalid_token when used, unknown or too old."""
    row = conn.execute("DELETE FROM line_logins WHERE claim_hash = %s AND created_at > now() - make_interval(mins => %s) "
                       "RETURNING user_id, merged_chat", (_hash(body.claim), LINE_LOGIN_MINUTES)).fetchone()
    if not row:
        raise ApiError(401, "invalid_token", "this LINE sign-in was already used or is too old")
    return envelope(conn, {"session_token": create_session(conn, row["user_id"]),
                           "user": _user_out(conn, _user_row(conn, row["user_id"])), "merged_chat": row["merged_chat"]})


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
    """PDPA erasure: the account, its sign-ins, sessions, places, memory, consents and messages."""
    tools.delete_user(conn, user["id"])
    return envelope(conn, {"deleted": True})
