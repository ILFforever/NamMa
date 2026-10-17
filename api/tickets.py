"""Rescue tickets for responders (docs/api.md → Rescue tickets). PDPA-sensitive: list shows the minimum,
detail and every change are written to ticket_events.

Stored vocabulary (agent/triage.py, agent/tools.py) is mapped to the contract enums on the way out.
Values with no contract equivalent (e.g. transport) are dropped, never invented.
"""
import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import psycopg
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from api.auth import require_responder
from api.common import ApiError, envelope, get_db

BANGKOK = ZoneInfo("Asia/Bangkok")
LIST_LIMIT = 500

STATUSES = ("open", "acknowledged", "in_progress", "resolved", "cancelled")   # contract; 'unconfirmed' is internal
VULNERABLE = ("elderly", "bedridden", "child", "infant", "pregnant", "disabled", "patient", "pet")
NEEDS = ("evacuation", "medical", "medication", "oxygen", "dialysis", "food_water", "boat", "power")

# Stored English values → contract values (no-op where they already match).
VULNERABLE_KEYS = {"elderly": "elderly", "bedridden": "bedridden", "child": "child", "children": "child",
                   "infant": "infant", "pregnant": "pregnant", "disabled": "disabled", "patient": "patient",
                   "pet": "pet", "pets": "pet"}
NEEDS_KEYS = {"evacuation": "evacuation", "medical": "medical", "medicine": "medication",
              "medication": "medication", "oxygen": "oxygen", "dialysis": "dialysis", "food": "food_water",
              "drinking_water": "food_water", "food_water": "food_water", "boat": "boat", "power": "power",
              "power_dependent": "power"}
# Thai free text the triage writes (e.g. "ยาย (เดินไม่ได้/ติดเตียง)", "ต้องฟอกไต"). Keyword rules, in order.
THAI_VULNERABLE = [("ติดเตียง|เดินไม่ได้", "bedridden"), ("ผู้สูงอายุ", "elderly"), ("ทารก|แรกเกิด|ลูกอ่อน", "infant"),
                   ("เด็ก", "child"), ("ตั้งครรภ์", "pregnant"), ("พิการ", "disabled"), ("ผู้ป่วย", "patient"),
                   ("สัตว์เลี้ยง", "pet")]
THAI_NEEDS = [("ฟอกไต", "dialysis"), ("ออกซิเจน", "oxygen"), ("ไฟฟ้า", "power")]
URGENCY_RANK = {"critical": 0, "high": 1, "normal": 2}

# Everything in the list view: no raw_text, contact_phone, assignee, notes (PDPA).
SUMMARY_COLS = ("id, place_label, lat, lon, people_count, vulnerable, needs, urgency, status, channel, "
                "created_at, updated_at")
FULL_COLS = SUMMARY_COLS + ", raw_text, contact_phone, assignee, notes"

router = APIRouter()


def _classify(value: str) -> tuple[set[str], set[str]]:
    """One stored string → (vulnerable values, needs values) in the contract vocabulary."""
    key = value.strip().lower()
    if key in VULNERABLE_KEYS:
        return {VULNERABLE_KEYS[key]}, set()
    if key in NEEDS_KEYS:
        return set(), {NEEDS_KEYS[key]}
    vuln = {v for pattern, v in THAI_VULNERABLE if re.search(pattern, value)}
    if "infant" in vuln:
        vuln.discard("child")          # "เด็กแรกเกิด" is an infant, not a child
    return vuln, {n for pattern, n in THAI_NEEDS if re.search(pattern, value)}


def _contract_lists(vulnerable, needs) -> tuple[list[str], list[str]]:
    """Stored JSONB lists → (vulnerable, needs), each in contract order, no duplicates.
    A stored item can land in either list: "ต้องฟอกไต" (needs dialysis) is written under vulnerable."""
    vuln_set: set[str] = set()
    need_set: set[str] = set()
    for value in [*(vulnerable if isinstance(vulnerable, list) else []),
                  *(needs if isinstance(needs, list) else [])]:
        if isinstance(value, str):
            v, n = _classify(value)
            vuln_set |= v
            need_set |= n
    return [x for x in VULNERABLE if x in vuln_set], [x for x in NEEDS if x in need_set]


def _urgency(stored: str | None) -> str:
    return stored if stored in URGENCY_RANK else "high"


def _as_bangkok(value: datetime) -> datetime:
    """Timestamps without an offset are read as Bangkok time (the API's display zone)."""
    return value if value.tzinfo else value.replace(tzinfo=BANGKOK)


def _summary(r: dict) -> dict:
    vulnerable, needs = _contract_lists(r["vulnerable"], r["needs"])
    return {
        "id": r["id"], "place_label": r["place_label"], "lat": r["lat"], "lon": r["lon"],
        "people_count": r["people_count"], "vulnerable": vulnerable, "needs": needs,
        "urgency": _urgency(r["urgency"]), "status": r["status"], "channel": r["channel"],
        "created_at": r["created_at"].isoformat(), "updated_at": r["updated_at"].isoformat(),
    }


def _full(r: dict) -> dict:
    return {**_summary(r), "raw_text": r["raw_text"], "contact_phone": r["contact_phone"],
            "assignee": r["assignee"], "notes": r["notes"]}


def _event(conn: psycopg.Connection, ticket_id: int, actor: str, action: str, detail: dict | None = None) -> None:
    conn.execute("INSERT INTO ticket_events (ticket_id, actor, action, detail) VALUES (%s, %s, %s, %s::jsonb)",
                 (ticket_id, actor, action, None if detail is None else json.dumps(detail, ensure_ascii=False)))


def _load(conn: psycopg.Connection, ticket_id: int, lock: bool = False) -> dict:
    """The ticket as responders may see it. Unconfirmed triage tickets do not exist for the API (404)."""
    sql = f"SELECT {FULL_COLS} FROM rescue_tickets WHERE id = %s AND status <> 'unconfirmed'"
    row = conn.execute(sql + (" FOR UPDATE" if lock else ""), (ticket_id,)).fetchone()
    if not row:
        raise ApiError(404, "not_found", "ticket not found")
    return row


@router.get("/api/tickets")
def list_tickets(status: str | None = None, since: str | None = None,
                 user: dict = Depends(require_responder), conn: psycopg.Connection = Depends(get_db)):
    if status is None:
        statuses = list(STATUSES)
    else:
        statuses = [s.strip() for s in status.split(",") if s.strip()] or list(STATUSES)
        bad = [s for s in statuses if s not in STATUSES]
        if bad:
            raise ApiError(400, "bad_request", f"unknown status: {', '.join(bad)}")
    sql = f"SELECT {SUMMARY_COLS} FROM rescue_tickets WHERE status = ANY(%s)"
    params: list = [statuses]
    if since is not None:
        try:
            params.append(_as_bangkok(datetime.fromisoformat(since)))
        except ValueError:
            raise ApiError(400, "bad_request", "since must be an ISO-8601 timestamp")
        sql += " AND updated_at > %s"
    sql += (" ORDER BY CASE urgency WHEN 'critical' THEN 0 WHEN 'high' THEN 1 ELSE 2 END, created_at, id "
            f"LIMIT {LIST_LIMIT}")
    rows = conn.execute(sql, params).fetchall()
    return envelope(conn, [_summary(r) for r in rows])


@router.get("/api/tickets/{ticket_id}")
def get_ticket(ticket_id: int, user: dict = Depends(require_responder), conn: psycopg.Connection = Depends(get_db)):
    row = _load(conn, ticket_id)
    _event(conn, ticket_id, str(user["id"]), "view")
    return envelope(conn, _full(row))


class TicketPatchIn(BaseModel):
    status: str | None = None
    assignee: str | None = None
    note: str | None = None
    updated_at: datetime


@router.patch("/api/tickets/{ticket_id}")
def patch_ticket(ticket_id: int, body: TicketPatchIn, user: dict = Depends(require_responder),
                 conn: psycopg.Connection = Depends(get_db)):
    if body.status is not None and body.status not in STATUSES:
        raise ApiError(400, "bad_request", f"status must be one of {', '.join(STATUSES)}")
    row = _load(conn, ticket_id, lock=True)
    if _as_bangkok(body.updated_at) != row["updated_at"]:
        raise ApiError(409, "conflict", "ticket changed since you loaded it")

    actor = str(user["id"])
    status, assignee, notes = row["status"], row["assignee"], row["notes"]
    events: list[tuple[str, dict]] = []
    if body.status is not None and body.status != status:
        events.append(("status", {"from": status, "to": body.status}))
        status = body.status
    if "assignee" in body.model_fields_set:
        new = body.assignee.strip() if body.assignee else None
        if new != assignee:
            events.append(("assign", {"from": assignee, "to": new}))
            assignee = new
    note = " ".join((body.note or "").split())          # one line: newlines would break the note format
    if note:
        stamp = datetime.now(BANGKOK).strftime("%H:%M")
        line = f"{stamp} {user['display_name'] or 'responder'}: {note}"
        notes = f"{notes}\n{line}" if notes else line
        events.append(("note", {"chars": len(note)}))

    if not events:
        return envelope(conn, _full(row))
    row = conn.execute(
        f"UPDATE rescue_tickets SET status = %s, assignee = %s, notes = %s, updated_at = now() "
        f"WHERE id = %s RETURNING {FULL_COLS}",
        (status, assignee, notes, ticket_id)).fetchone()
    for action, detail in events:
        _event(conn, ticket_id, actor, action, detail)
    return envelope(conn, _full(row))
