"""What NamMa remembers about the person chatting (docs/api.md → Profile): saved places, stated facts, consent.

The web ProfilePanel shows it beside the chat, renames places and names, and deletes from it (CLAUDE.md §7: user control). Who is asking:
the signed-in user, else the anonymous web chat session from the X-Chat-Session header (the same session_id
POST /api/chat uses, so the panel shows what that chat saved). Nothing is created here: no user, empty profile.
"""
import psycopg
from fastapi import APIRouter, Depends, Header
from psycopg.rows import tuple_row
from pydantic import BaseModel, Field

from agent import memory, tools
from api.auth import current_user
from api.common import ApiError, envelope, get_db

router = APIRouter()

FACT_KINDS = ("household", "needs", "house", "assets", "history")
APPROX_PREFIX = "ประมาณจากชื่อพื้นที่:"


class PlaceRename(BaseModel):
    label: str = Field(max_length=40)


class NameRename(BaseModel):
    list: str = Field(max_length=20)            # one of memory.NAME_LISTS
    from_: str = Field(alias="from", max_length=200)
    to: str = Field(max_length=60)


def _new_name(raw: str) -> str:
    name = " ".join(raw.split())
    if not name:
        raise ApiError(400, "bad_request", "name is empty")
    return name


def _user_id(conn: psycopg.Connection, user: dict | None, chat_session: str | None) -> int | None:
    """The signed-in account, else the anonymous web chat's user (if that chat has said anything yet)."""
    conn.row_factory = tuple_row   # agent code reads rows by position (row[0])
    if user:
        return user["id"]
    if not chat_session or len(chat_session) > 128:
        return None
    row = conn.execute("SELECT id FROM users WHERE line_user_id = %s", (f"web:{chat_session}",)).fetchone()
    return row[0] if row else None


def _where(amphoe: str | None, province: str | None) -> str | None:
    """'อ.บางบาล จ.พระนครศรีอยุธยา'; Bangkok districts are เขต, and Bangkok has no จ."""
    if not province:
        return None
    if province.startswith("กรุงเทพ"):
        return f"เขต{amphoe} กรุงเทพมหานคร" if amphoe else "กรุงเทพมหานคร"
    return f"อ.{amphoe} จ.{province}" if amphoe else f"จ.{province}"


def _place(conn: psycopg.Connection, loc: dict) -> dict:
    notes = loc["notes"] or ""
    approx = notes.startswith("ประมาณ")
    near = tools.get_nearest_stations(conn, loc["lat"], loc["lon"], k=1, fill=False)
    s = near[0] if near else None
    where = notes.removeprefix(APPROX_PREFIX).strip() if notes.startswith(APPROX_PREFIX) else None
    if where is None and s:
        where = _where(s["amphoe"], s["province"])
    return {
        "id": loc["id"], "label": loc["label"], "lat": loc["lat"], "lon": loc["lon"], "approx": approx, "where": where,
        "nearest": {"station_id": s["station_id"], "code": s["code"], "name_th": s["name_th"],
                    "distance_km": s["distance_km"], "risk_tier": s["risk_tier"],
                    "diff_wl_bank": s["diff_wl_bank"], "observed_at": s["reading_ts"]} if s else None,
    }


def _profile(conn: psycopg.Connection, uid: int | None, line_linked: bool) -> dict:
    empty = {"places": [], "facts": [], "consent": {"granted": False, "ts": None}, "pending_consent": False,
             "line_linked": line_linked}
    if uid is None:
        return empty
    prof = tools.get_user_profile(conn, uid)
    mem = prof["memory"] or {}
    facts = [{"kind": k, "text": line, "sensitive": k in memory.SENSITIVE, "list": lst, "names": names}
             for k in FACT_KINDS for line, lst, names in memory.describe_items({k: mem.get(k) or {}})]
    consent = conn.execute("SELECT granted, ts FROM consents WHERE user_id = %s AND kind = 'sensitive_memory' "
                           "ORDER BY ts DESC LIMIT 1", (uid,)).fetchone()
    return {
        "places": [_place(conn, loc) for loc in prof["locations"]],
        "facts": facts,
        "consent": {"granted": bool(consent and consent[0]), "ts": consent[1] if consent else None},
        "pending_consent": bool(prof["pending"]),
        "line_linked": line_linked,
    }


@router.get("/api/profile")
def get_profile(user: dict | None = Depends(current_user), x_chat_session: str | None = Header(None),
                conn: psycopg.Connection = Depends(get_db)):
    uid = _user_id(conn, user, x_chat_session)
    return envelope(conn, _profile(conn, uid, line_linked=bool(user and tools.is_line_id(user["line_user_id"]))))


@router.delete("/api/profile/places/{place_id}")
def delete_place(place_id: int, user: dict | None = Depends(current_user), x_chat_session: str | None = Header(None),
                 conn: psycopg.Connection = Depends(get_db)):
    uid = _user_id(conn, user, x_chat_session)
    if uid is None or not conn.execute("SELECT 1 FROM user_locations WHERE id = %s AND user_id = %s",
                                       (place_id, uid)).fetchone():
        raise ApiError(404, "not_found", f"place {place_id} not found")
    tools.forget(conn, uid, f"location:{place_id}")
    return envelope(conn, _profile(conn, uid, line_linked=bool(user and tools.is_line_id(user["line_user_id"]))))


@router.patch("/api/profile/places/{place_id}")
def rename_place(place_id: int, body: PlaceRename, user: dict | None = Depends(current_user),
                 x_chat_session: str | None = Header(None), conn: psycopg.Connection = Depends(get_db)):
    """A place is known by its label in chat and in LINE alerts, so two places can't share one (save_location
    upserts by label): 409 when another place already has it."""
    label = _new_name(body.label)
    uid = _user_id(conn, user, x_chat_session)
    if uid is None or not conn.execute("SELECT 1 FROM user_locations WHERE id = %s AND user_id = %s",
                                       (place_id, uid)).fetchone():
        raise ApiError(404, "not_found", f"place {place_id} not found")
    if conn.execute("SELECT 1 FROM user_locations WHERE user_id = %s AND label = %s AND id <> %s",
                    (uid, label, place_id)).fetchone():
        raise ApiError(409, "label_taken", f"another place is already called {label}")
    conn.execute("UPDATE user_locations SET label = %s WHERE id = %s", (label, place_id))
    return envelope(conn, _profile(conn, uid, line_linked=bool(user and tools.is_line_id(user["line_user_id"]))))


@router.patch("/api/profile/names")
def rename_name(body: NameRename, user: dict | None = Depends(current_user), x_chat_session: str | None = Header(None),
                conn: psycopg.Connection = Depends(get_db)):
    """Rename one name the person gave (a person who can't walk, a pet, a past flood): Profile facts[].names.
    The rest of memory is a fixed checklist (มีรถยนต์, บ้าน 2 ชั้น) with nothing to rename."""
    field = memory.NAME_LISTS.get(body.list)
    if field is None:
        raise ApiError(400, "bad_request", f"unknown list: {body.list}")
    to = _new_name(body.to)
    uid = _user_id(conn, user, x_chat_session)
    row = conn.execute(f"SELECT {field} FROM user_memory WHERE user_id = %s", (uid,)).fetchone() if uid else None
    names = list(((row[0] if row else None) or {}).get(body.list) or [])
    if body.from_ not in names:
        raise ApiError(404, "not_found", f"{body.list} has no {body.from_}")
    if to != body.from_:
        if to in names:
            raise ApiError(409, "name_taken", f"{body.list} already has {to}")
        names[names.index(body.from_)] = to
        tools.merge_memory(conn, uid, {field: {body.list: names}})
    return envelope(conn, _profile(conn, uid, line_linked=bool(user and tools.is_line_id(user["line_user_id"]))))


@router.delete("/api/profile/facts/{kind}")
def delete_facts(kind: str, user: dict | None = Depends(current_user), x_chat_session: str | None = Header(None),
                 conn: psycopg.Connection = Depends(get_db)):
    """kind: one of FACT_KINDS, or 'all' (every fact, place and fact waiting for consent)."""
    if kind not in (*FACT_KINDS, "all"):
        raise ApiError(400, "bad_request", f"unknown kind: {kind}")
    uid = _user_id(conn, user, x_chat_session)
    if uid is not None:
        tools.forget(conn, uid, kind)
    return envelope(conn, _profile(conn, uid, line_linked=bool(user and tools.is_line_id(user["line_user_id"]))))
