"""Alert job (CLAUDE.md §8): after each risk refresh, push when a saved location's tier rises.

Push only on a tier increase, or every 6 h while at danger. Quiet hours hold back everything but
danger. Web-only users get the alert as a message in their chat history instead of a LINE push.
"""
from datetime import datetime, timedelta

from psycopg.rows import dict_row

from agent import answer, memory, tools
from agent.risk import TIER_TH, rank
from line import client as line

DANGER_COOLDOWN = timedelta(hours=6)
NEAR_KM = 15
HEADER = {"watch": "🟡 เฝ้าระวัง", "warning": "🟠 เตือนภัยน้ำ", "danger": "🔴 อันตราย: น้ำล้นตลิ่ง"}


def in_quiet_hours(prefs: dict, now: datetime) -> bool:
    q = prefs.get("quiet_hours")      # [start_hour, end_hour], e.g. [22, 6]
    if not q:
        return False
    start, end = q
    h = now.hour
    return start <= h or h < end if start > end else start <= h < end


def decide(tier: str, last_tier: str | None, last_ts: datetime | None, now: datetime, quiet: bool) -> str | None:
    """'push', 'record' (silent tier drop, so a later rise alerts again) or None."""
    if last_tier is None:
        action = "push" if rank(tier) >= rank("watch") else None
    elif rank(tier) > rank(last_tier):
        action = "push"
    elif tier == "danger" and last_tier == "danger" and now - last_ts >= DANGER_COOLDOWN:
        action = "push"
    elif rank(tier) < rank(last_tier):
        action = "record"
    else:
        action = None
    if action == "push" and quiet and tier != "danger":
        return None                    # retried on the next run, after quiet hours
    return action


def alert_text(label: str, station: dict, mem: dict, shelters: list[dict]) -> str:
    tier = station["risk_tier"]
    lines = [f"{HEADER.get(tier, TIER_TH[tier])} — {label}"]
    if (mem.get("household") or {}).get("immobile") and rank(tier) >= rank("warning"):
        lines.append("‼️ บ้านนี้มีคนที่เดินไม่ได้ ควรเริ่มย้ายไปที่ปลอดภัยตั้งแต่ตอนนี้ ไม่ต้องรอน้ำเข้าบ้าน")
    lines += answer.station_lines(station)
    lines.append("⚠️ ค่าที่สถานีริมแม่น้ำ ไม่ใช่ระดับน้ำที่บ้านโดยตรง")
    lines.append("\n✅ สิ่งที่ควรทำตอนนี้")
    lines += [f"• {a}" for a in answer.rule_advice(tier, mem)]
    sh = answer.shelter_line(shelters, need_patients=bool((mem.get("household") or {}).get("immobile")))
    if sh:
        lines += [""] + sh
    return "\n".join(lines) + "\n\n" + answer.FOOTER


def run(conn, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now().astimezone()
    with conn.cursor(row_factory=dict_row) as cur:
        locs = cur.execute("SELECT l.*, u.line_user_id FROM user_locations l JOIN users u ON u.id = l.user_id").fetchall()
    sent = []
    for loc in locs:
        if conn.execute("SELECT granted FROM consents WHERE user_id = %s AND kind = 'alerts' ORDER BY ts DESC LIMIT 1",
                        (loc["user_id"],)).fetchone() == (False,):
            continue
        stations = tools.get_nearest_stations(conn, loc["lat"], loc["lon"], k=3)
        if not stations:
            continue
        near = [s for s in stations if s["distance_km"] <= NEAR_KM] or stations[:1]
        st = max(near, key=lambda s: rank(s["risk_tier"]))
        last = conn.execute("SELECT risk_tier, ts FROM alerts_sent WHERE location_id = %s ORDER BY ts DESC LIMIT 1",
                            (loc["id"],)).fetchone()
        prof = tools.get_user_profile(conn, loc["user_id"])
        quiet = in_quiet_hours(prof["memory"].get("preferences") or {}, now)
        action = decide(st["risk_tier"], last[0] if last else None, last[1] if last else None, now, quiet)
        if not action:
            continue
        if action == "push":
            mem = prof["memory"]
            hh = mem.get("household") or {}
            shelters = tools.find_shelters(conn, loc["lat"], loc["lon"],
                                           {"patients": bool(hh.get("immobile")), "pets": bool(hh.get("pets"))})
            text = alert_text(loc["label"], st, mem, shelters)
            uid_ext = loc["line_user_id"] or ""
            if not uid_ext.startswith("web:") and line.configured():
                try:
                    line.push(uid_ext, [line.text_message(text)])
                except Exception as e:
                    print(f"push failed for location {loc['id']}: {e!r}", flush=True)
                    continue
            tools.log_message(conn, loc["user_id"], "alert", "assistant", text)
            sent.append({"location_id": loc["id"], "station": st["code"], "tier": st["risk_tier"]})
        conn.execute("INSERT INTO alerts_sent (user_id, location_id, station_id, risk_tier, ts, pushed) "
                     "VALUES (%s, %s, %s, %s, %s, %s)",
                     (loc["user_id"], loc["id"], st["station_id"], st["risk_tier"], now, action == "push"))
    conn.commit()
    return sent
