"""One turn of conversation, channel-agnostic. LINE and the web app both call handle().

Flow (CLAUDE.md §6, manual tool flow so it works with or without MaaS function calling):
classify intent in code → call tools → template facts (+ optional LLM advice) → reply.
"""
import re
from dataclasses import dataclass, field

from agent import answer, memory, tools, triage
from agent.geo import find_place
from agent.risk import rank

CONSENT_YES, CONSENT_NO = "ยินยอมให้จำข้อมูล", "ไม่ยินยอม"
CONFIRM_RE = re.compile(r"ยืนยันขอความช่วยเหลือ\s*#(\d+)")
CONFIRM_TEXT = "ยืนยันส่งคำขอ"      # confirm the newest unconfirmed ticket (web postback triage:confirm)
CANCEL_TEXT = "ยกเลิกคำขอ"          # cancel the newest unconfirmed ticket (web postback triage:cancel)
FORGET_ALL_TEXT = "ลบข้อมูลทั้งหมด"  # same text as the profile quick reply (web postback memory:delete_all)
CONSENT_PROMPT = ("🔒 ขออนุญาตจำข้อมูลนี้ เพื่อแจ้งเตือนและช่วยเหลือได้เร็วขึ้นไหม?\n"
                  "ข้อมูลสุขภาพเป็นข้อมูลอ่อนไหวตาม PDPA ถ้าไม่ยินยอม จะใช้เฉพาะในบทสนทนานี้")
HELP = ("สวัสดีค่ะ น้ำมาช่วยดูความเสี่ยงน้ำท่วมจากข้อมูลสถานีวัดน้ำจริง\n"
        "ลองพิมพ์ เช่น \"บ้านแม่อยู่บางบาล อยุธยา น้ำจะถึงไหม\" หรือกดแชร์ตำแหน่ง\n"
        "ถ้าอยู่ในอันตราย พิมพ์ \"ช่วยด้วย\" ได้ทันที\n\n" + answer.FOOTER)
RISK_WORDS = ("น้ำ", "ท่วม", "ตลิ่ง", "ระดับ", "อพยพ", "เสี่ยง")
RESCUE_HOTLINE = "🚨 ถ้าอันตรายตอนนี้ โทร 1784 (ปภ.) หรือ 1669 (เจ็บป่วยฉุกเฉิน) ทันที • LINE @1784DDPM"
# Pictographs and their joiners/variation selectors. Thai (U+0E00 block), "•", "—" and "±" are outside these ranges.
_EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2139\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D] ?")


@dataclass
class QuickReply:
    label: str               # LINE limits labels to 20 chars
    text: str | None = None  # message to send back; None + kind='location' → share-location button
    kind: str = "message"


@dataclass
class Reply:
    text: str
    quick: list[QuickReply] = field(default_factory=list)
    ticket_id: int | None = None
    sources: list[dict] = field(default_factory=list)   # docs/api.md Source objects (used by the web API)
    emergency: bool = False                             # danger signalled, or a ticket was created/confirmed


def _q(label, text=None):
    return QuickReply(label[:20], text or label)


LOCATION_Q = QuickReply("แชร์ตำแหน่ง", kind="location")
PROFILE_Q = _q("ข้อมูลที่จำไว้", "ข้อมูลอะไรที่จำเกี่ยวกับฉัน")


def handle(conn, external_id: str, channel: str, text: str | None = None,
           location: tuple[float, float] | None = None, display_name: str | None = None) -> Reply:
    uid = tools.get_or_create_user(conn, external_id, display_name)
    if text:
        tools.log_message(conn, uid, channel, "user", text)
    elif location:
        tools.log_message(conn, uid, channel, "user", f"[ตำแหน่ง {location[0]:.5f},{location[1]:.5f}]")
    reply = _route(conn, uid, text or "", location, channel)
    if channel == "web":
        reply = web_reply(reply)
    tools.log_message(conn, uid, channel, "assistant", reply.text)
    conn.commit()
    return reply


def web_reply(reply: Reply) -> Reply:
    """The web app draws the hotline itself (HotlineNotice under answers with a station source, EmergencyCard
    above emergency replies), so drop the text copies it would duplicate, and the emoji LINE needs but the
    web design system does not. Hotline mentions inside sentences stay. LINE output is never passed through here.
    """
    text = reply.text
    if reply.emergency:
        text = text.removeprefix(RESCUE_HOTLINE + "\n\n")
    elif any(s["type"] == "station" for s in reply.sources):
        text = text.removesuffix("\n\n" + answer.FOOTER)
    text = "\n".join(_EMOJI.sub("", ln).strip() for ln in text.split("\n"))
    return Reply(text, reply.quick, reply.ticket_id, reply.sources, reply.emergency)


def _route(conn, uid: int, text: str, location, channel: str = "line") -> Reply:
    t = text.strip()
    if t == CONSENT_YES:
        tools.record_consent(conn, uid, True)
        facts = tools.pop_pending(conn, uid)
        tools.merge_memory(conn, uid, facts)
        lines = memory.describe(facts)
        return Reply("ขอบคุณค่ะ บันทึกแล้ว: " + (", ".join(lines) if lines else "-") +
                     "\nดูหรือลบได้ตลอด พิมพ์ \"ข้อมูลอะไรที่จำเกี่ยวกับฉัน\"", [PROFILE_Q])
    if t == CONSENT_NO:
        tools.record_consent(conn, uid, False)
        return Reply("ได้ค่ะ จะไม่บันทึกข้อมูลนี้ จะใช้เฉพาะในบทสนทนานี้เท่านั้น")
    if t == CONFIRM_TEXT:
        tid = pending_ticket_id(conn, uid)
        return _confirm(conn, uid, tid) if tid else _no_pending()
    if m := CONFIRM_RE.search(t):
        return _confirm(conn, uid, int(m.group(1)))
    if t == CANCEL_TEXT:
        return _cancel(conn, uid)
    if location:
        return _on_location(conn, uid, location)
    if triage.is_danger(t):
        return _rescue(conn, uid, t, channel)
    if re.search(r"จำอะไร|ข้อมูลอะไรที่จำ|ข้อมูลของฉัน|โปรไฟล์", t):
        return _profile(conn, uid)
    if t.startswith("ลบ"):
        return _forget(conn, uid, t)
    return _talk(conn, uid, t)


def _remember(conn, uid: int, text: str) -> tuple[dict, bool]:
    """Extract stated facts; save the non-sensitive ones, park sensitive ones. Returns (facts, needs_consent)."""
    facts = memory.extract(text)
    if not facts:
        return {}, False
    plain, sensitive = memory.split_sensitive(facts)
    tools.merge_memory(conn, uid, plain)
    if sensitive:
        if tools.has_consent(conn, uid):
            tools.merge_memory(conn, uid, sensitive)
        else:
            tools.set_pending(conn, uid, sensitive)
            return facts, True
    return facts, False


def _consent_quick() -> list[QuickReply]:
    return [_q("ยินยอม", CONSENT_YES), _q(CONSENT_NO)]


def _assess(conn, uid: int, lat: float, lon: float, label: str, approx: bool,
            extra: list[str] | None = None) -> tuple[str, list[dict]]:
    """Reply text plus the sources (stations, shelter, GISTDA) whose numbers that text shows."""
    prof = tools.get_user_profile(conn, uid)
    mem = memory.merged_view(prof["memory"], prof["pending"])
    stations = tools.get_nearest_stations(conn, lat, lon, k=3)
    hh = mem.get("household") or {}
    shelters = tools.find_shelters(conn, lat, lon, {"patients": bool(hh.get("immobile")), "pets": bool(hh.get("pets"))})
    extent = tools.check_flood_extent(lat, lon)
    recurrence = tools.get_flood_recurrence(lat, lon)
    text = answer.risk_answer(label, approx, stations, shelters, mem, extent, recurrence, extra)
    return text, _sources(stations, shelters, extent, recurrence)


def _sources(stations: list[dict], shelters: list[dict], extent: dict | None, recurrence: dict | None) -> list[dict]:
    """Mirrors what answer.risk_answer prints: main station (+ the worse one if shown), first shelter, GISTDA lines."""
    if not stations:            # risk_answer prints no numbers at all without a station
        return []
    main = stations[0]
    worst = max(stations, key=lambda s: rank(s["risk_tier"]))
    shown = [main] + ([worst] if worst is not main and rank(worst["risk_tier"]) > rank(main["risk_tier"]) else [])
    out = [_station_source(s) for s in shown]
    out += [{"type": "shelter", "shelter_id": sh["id"], "name": sh["name"], "distance_km": sh["distance_km"]}
            for sh in shelters[:1]]
    if extent is not None:
        out.append({"type": "gistda", "kind": "flood_extent", "observed_at": None,
                    "summary": "พบพื้นที่น้ำท่วมที่จุดนี้ (ภาพดาวเทียม 1 วันล่าสุด)" if extent["flooded_last_1day"]
                    else "ไม่พบน้ำท่วมที่จุดนี้ (ภาพดาวเทียม 1 วันล่าสุด)"})
    if recurrence is not None and recurrence.get("records"):
        out.append({"type": "gistda", "kind": "flood_recurrence", "observed_at": None,
                    "summary": f"จุดนี้เคยมีบันทึกน้ำท่วม {recurrence['records']} ครั้ง (2011–2023)"})
    return out


def _station_source(s: dict) -> dict:
    """A station Source with the reading the web StationCard draws. Every number is from get_nearest_stations."""
    level, diff = s.get("level_msl"), s.get("diff_wl_bank")
    return {"type": "station", "station_id": s["station_id"], "code": s["code"], "name_th": s["name_th"],
            "distance_km": s["distance_km"], "observed_at": s["reading_ts"],
            "river": s.get("river"), "agency": s.get("agency"),
            "amphoe": s.get("amphoe"), "province": s.get("province"),
            "level_msl": level, "diff_wl_bank": diff,
            "bank_msl": round(level - diff, 2) if level is not None and diff is not None else None,
            "trend_cm_per_hr": s.get("trend_3h_cm_per_hr"), "hours_to_bank": s.get("hours_to_bank"),
            "risk_tier": s.get("risk_tier"),
            "upstream_over": [u["oldcode"] for u in s.get("upstream") or [] if u.get("risk_tier") == "danger"]}


def _talk(conn, uid: int, t: str) -> Reply:
    facts, need_consent = _remember(conn, uid, t)
    place = find_place(t)
    label = memory.location_label(t)
    prof = tools.get_user_profile(conn, uid)
    target = None
    if place:
        target = (place.lat, place.lon, (label + " " if label else "") + place.label, True)
        if label:
            tools.save_location(conn, uid, label, place.lat, place.lon, notes=f"ประมาณจากชื่อพื้นที่: {place.label}")
    elif prof["locations"] and any(w in t for w in RISK_WORDS):
        loc = next((l for l in prof["locations"] if l["label"] in t), prof["locations"][0])
        target = (loc["lat"], loc["lon"], loc["label"], bool(loc["notes"] and loc["notes"].startswith("ประมาณ")))

    if target:
        extra = []
        if label and place:
            extra.append(f"📌 บันทึก \"{label}\" ไว้แล้ว ถ้าน้ำที่สถานีใกล้ๆ สูงขึ้น จะส่งข้อความเตือน")
        if not prof["memory"].get("household") and not prof["pending"]:
            extra.append("👪 ที่บ้านมีผู้สูงอายุ คนที่เดินไม่ได้ เด็กเล็ก หรือสัตว์เลี้ยงไหม? บอกได้ จะช่วยวางแผนให้เหมาะกับครอบครัว")
        quick = [LOCATION_Q, PROFILE_Q]
        if need_consent:
            extra.append(CONSENT_PROMPT)
            quick = _consent_quick()
        text, sources = _assess(conn, uid, target[0], target[1], target[2], target[3], extra)
        return Reply(text, quick, sources=sources)

    if facts:
        lines = memory.describe(facts)
        if need_consent:
            return Reply("รับทราบค่ะ: " + ", ".join(lines) + "\n\n" + CONSENT_PROMPT, _consent_quick())
        return Reply("รับทราบและบันทึกแล้วค่ะ: " + ", ".join(lines), [PROFILE_Q])
    if any(w in t for w in RISK_WORDS):
        return Reply("บอกชื่อตำบล/อำเภอ/จังหวัด หรือกดแชร์ตำแหน่ง แล้วจะเช็คสถานีวัดน้ำใกล้ที่สุดให้ค่ะ", [LOCATION_Q])
    return Reply(HELP, [LOCATION_Q, _q("บ้านอยู่บางบาล อยุธยา น้ำจะถึงไหม")])


def _on_location(conn, uid: int, location) -> Reply:
    lat, lon = location
    # A GPS fix beats the place-name or saved-place guess triage started with, so it replaces the ticket's
    # coordinates; an unconfirmed ticket is sent with it. Tickets being worked on keep their status.
    open_ticket = conn.execute(
        "SELECT id, status FROM rescue_tickets WHERE user_id = %s AND created_at > now() - interval '1 hour' "
        "AND status NOT IN ('resolved', 'cancelled') ORDER BY id DESC LIMIT 1", (uid,)).fetchone()
    if open_ticket:
        tid, status = open_ticket
        tools.update_ticket(conn, tid, lat=lat, lon=lon, status="open" if status == "unconfirmed" else status)
        return Reply(f"📍 ได้รับตำแหน่งแล้ว ส่งคำขอ #{tid} พร้อมพิกัดถึงทีมช่วยเหลือแล้ว\n"
                     "☎️ โทร 1784 ซ้ำได้ถ้าสถานการณ์แย่ลง", ticket_id=tid, emergency=True)
    prof = tools.get_user_profile(conn, uid)
    label = "บ้าน" if not prof["locations"] else "ตำแหน่งที่แชร์"
    tools.save_location(conn, uid, label, lat, lon)
    text, sources = _assess(conn, uid, lat, lon, label, approx=False,
                            extra=[f"📌 บันทึกตำแหน่งนี้เป็น \"{label}\" แล้ว จะแจ้งเตือนเมื่อน้ำสูงขึ้น"])
    return Reply(text, [PROFILE_Q], sources=sources)


def _rescue(conn, uid: int, t: str, channel: str = "line") -> Reply:
    _remember(conn, uid, t)   # parks sensitive facts; triage still uses them for this ticket
    prof = tools.get_user_profile(conn, uid)
    mem = memory.merged_view(prof["memory"], prof["pending"])
    f = triage.extract(t, mem)
    place = find_place(t)
    if place:
        lat, lon, place_label = place.lat, place.lon, place.label
    elif prof["locations"]:
        saved = prof["locations"][0]
        lat, lon, place_label = saved["lat"], saved["lon"], saved["label"]
    else:
        lat, lon, place_label = None, None, None
    tid = tools.create_rescue_ticket(conn, uid, t, lat, lon, f["people_count"], f["vulnerable"], f["needs"],
                                     f["urgency"], status="unconfirmed", channel=channel, place_label=place_label)
    lines = [RESCUE_HOTLINE,
             "", f"น้ำมาเตรียมคำขอความช่วยเหลือ #{tid} ให้ทีมช่วยเหลือแล้ว:",
             f"• จำนวนคน: {f['people_count'] or 'ไม่ระบุ'}",
             f"• ผู้ที่ต้องดูแลพิเศษ: {', '.join(f['vulnerable']) or 'ไม่ระบุ'}",
             f"• ต้องการ: {', '.join(triage.NEEDS_TH.get(n, n) for n in f['needs'])}",
             f"• ความเร่งด่วน: {'วิกฤต' if f['urgency'] == 'critical' else 'สูง'}",
             f"• ตำแหน่ง: {f'ประมาณจาก {place_label} แชร์ตำแหน่งเพื่อให้แม่นขึ้น' if lat is not None else 'ยังไม่มี กรุณาแชร์ตำแหน่ง'}",
             "", "ระหว่างรอ: ขึ้นที่สูง ปิดไฟฟ้า อยู่ห่างกระแสน้ำ เปิดโทรศัพท์ไว้"]
    quick = [_q("ยืนยันส่งคำขอ", f"ยืนยันขอความช่วยเหลือ #{tid}"), LOCATION_Q]
    return Reply("\n".join(lines), quick, ticket_id=tid, emergency=True)


def pending_ticket_id(conn, uid: int) -> int | None:
    """The newest ticket this person has not confirmed yet (created in the last hour), if any."""
    row = conn.execute("SELECT id FROM rescue_tickets WHERE user_id = %s AND status = 'unconfirmed' "
                       "AND created_at > now() - interval '1 hour' ORDER BY id DESC LIMIT 1", (uid,)).fetchone()
    return row[0] if row else None


def _no_pending() -> Reply:
    return Reply("ไม่พบคำขอความช่วยเหลือที่รอยืนยันค่ะ ถ้าต้องการความช่วยเหลือ โทร 1784 ได้ทันที", [LOCATION_Q])


def _confirm(conn, uid: int, tid: int) -> Reply:
    """Only the owner's ticket, and only while it is still unconfirmed."""
    row = conn.execute("SELECT status FROM rescue_tickets WHERE id = %s AND user_id = %s", (tid, uid)).fetchone()
    if not row or row[0] != "unconfirmed":
        return Reply(f"ไม่พบคำขอ #{tid} ที่รอยืนยันค่ะ", [LOCATION_Q])
    tools.update_ticket(conn, tid, status="open")
    return Reply(f"✅ ส่งคำขอความช่วยเหลือ #{tid} ถึงทีมช่วยเหลือแล้ว\n"
                 "ระหว่างรอ: อยู่ที่สูง เปิดโทรศัพท์ไว้ และโทร 1784 ซ้ำได้ถ้าสถานการณ์แย่ลง",
                 [LOCATION_Q], ticket_id=tid, emergency=True)


def _cancel(conn, uid: int) -> Reply:
    tid = pending_ticket_id(conn, uid)
    if not tid:
        return _no_pending()
    tools.update_ticket(conn, tid, status="cancelled")
    return Reply(f"ยกเลิกคำขอ #{tid} แล้ว ถ้าต้องการความช่วยเหลือ โทร 1784 ได้ทันที")


def _profile(conn, uid: int) -> Reply:
    prof = tools.get_user_profile(conn, uid)
    lines = memory.describe(prof["memory"])
    locs = [f"📌 {l['label']}" for l in prof["locations"]]
    if not lines and not locs:
        return Reply("ตอนนี้ยังไม่ได้จำข้อมูลอะไรเกี่ยวกับคุณค่ะ")
    text = "ข้อมูลที่น้ำมาจำไว้:\n" + "\n".join(locs + [f"• {x}" for x in lines])
    quick = [_q(f"ลบ {l['label']}") for l in prof["locations"][:5]]
    if prof["memory"].get("household") or prof["memory"].get("needs"):
        quick.append(_q("ลบข้อมูลสุขภาพ"))
    quick.append(_q("ลบข้อมูลทั้งหมด"))
    return Reply(text, quick)


def _forget(conn, uid: int, t: str) -> Reply:
    target = t[2:].strip()
    if target in ("ข้อมูลทั้งหมด", "ทั้งหมด"):
        tools.forget(conn, uid, "all")
        return Reply("ลบข้อมูลทั้งหมดแล้วค่ะ")
    if target == "ข้อมูลสุขภาพ":
        tools.forget(conn, uid, "household")
        tools.forget(conn, uid, "needs")
        return Reply("ลบข้อมูลสุขภาพและสมาชิกในบ้านแล้วค่ะ")
    for loc in tools.get_user_profile(conn, uid)["locations"]:
        if loc["label"] == target:
            tools.forget(conn, uid, f"location:{loc['id']}")
            return Reply(f"ลบ \"{target}\" แล้ว จะไม่แจ้งเตือนสำหรับที่นี่อีก")
    return Reply("ไม่พบข้อมูลนั้นค่ะ", [PROFILE_Q])
