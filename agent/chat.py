"""One turn of conversation, channel-agnostic. LINE and the web app both call handle().

Flow (CLAUDE.md §6, manual tool flow so it works with or without MaaS function calling):
classify intent in code → call tools → template facts (+ optional LLM advice) → reply.
"""
import re
from dataclasses import dataclass, field

from agent import answer, memory, tools, triage
from agent.geo import find_place

CONSENT_YES, CONSENT_NO = "ยินยอมให้จำข้อมูล", "ไม่ยินยอม"
CONFIRM_RE = re.compile(r"ยืนยันขอความช่วยเหลือ\s*#(\d+)")
CONSENT_PROMPT = ("🔒 ขออนุญาตจำข้อมูลนี้ เพื่อแจ้งเตือนและช่วยเหลือได้เร็วขึ้นไหม?\n"
                  "ข้อมูลสุขภาพเป็นข้อมูลอ่อนไหวตาม PDPA ถ้าไม่ยินยอม จะใช้เฉพาะในบทสนทนานี้")
HELP = ("สวัสดีค่ะ น้ำมาช่วยดูความเสี่ยงน้ำท่วมจากข้อมูลสถานีวัดน้ำจริง\n"
        "ลองพิมพ์ เช่น \"บ้านแม่อยู่บางบาล อยุธยา น้ำจะถึงไหม\" หรือกดแชร์ตำแหน่ง\n"
        "ถ้าอยู่ในอันตราย พิมพ์ \"ช่วยด้วย\" ได้ทันที\n\n" + answer.FOOTER)
RISK_WORDS = ("น้ำ", "ท่วม", "ตลิ่ง", "ระดับ", "อพยพ", "เสี่ยง")


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
    reply = _route(conn, uid, text or "", location)
    tools.log_message(conn, uid, channel, "assistant", reply.text)
    conn.commit()
    return reply


def _route(conn, uid: int, text: str, location) -> Reply:
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
    if m := CONFIRM_RE.search(t):
        tools.update_ticket(conn, int(m.group(1)), status="open")
        return Reply(f"✅ ส่งคำขอความช่วยเหลือ #{m.group(1)} ถึงทีมช่วยเหลือแล้ว\n"
                     "ระหว่างรอ: อยู่ที่สูง เปิดโทรศัพท์ไว้ และโทร 1784 ซ้ำได้ถ้าสถานการณ์แย่ลง", [LOCATION_Q])
    if location:
        return _on_location(conn, uid, location)
    if triage.is_danger(t):
        return _rescue(conn, uid, t)
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


def _assess(conn, uid: int, lat: float, lon: float, label: str, approx: bool, extra: list[str] | None = None) -> str:
    prof = tools.get_user_profile(conn, uid)
    mem = memory.merged_view(prof["memory"], prof["pending"])
    stations = tools.get_nearest_stations(conn, lat, lon, k=3)
    hh = mem.get("household") or {}
    shelters = tools.find_shelters(conn, lat, lon, {"patients": bool(hh.get("immobile")), "pets": bool(hh.get("pets"))})
    return answer.risk_answer(label, approx, stations, shelters, mem,
                              tools.check_flood_extent(lat, lon), tools.get_flood_recurrence(lat, lon), extra)


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
        return Reply(_assess(conn, uid, target[0], target[1], target[2], target[3], extra), quick)

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
    open_ticket = conn.execute(
        "SELECT id FROM rescue_tickets WHERE user_id = %s AND lat IS NULL AND created_at > now() - interval '1 hour' "
        "ORDER BY id DESC LIMIT 1", (uid,)).fetchone()
    if open_ticket:
        tools.update_ticket(conn, open_ticket[0], lat=lat, lon=lon, status="open")
        return Reply(f"📍 ได้รับตำแหน่งแล้ว ส่งคำขอ #{open_ticket[0]} พร้อมพิกัดถึงทีมช่วยเหลือแล้ว\n"
                     "☎️ โทร 1784 ซ้ำได้ถ้าสถานการณ์แย่ลง")
    prof = tools.get_user_profile(conn, uid)
    label = "บ้าน" if not prof["locations"] else "ตำแหน่งที่แชร์"
    tools.save_location(conn, uid, label, lat, lon)
    text = _assess(conn, uid, lat, lon, label, approx=False,
                   extra=[f"📌 บันทึกตำแหน่งนี้เป็น \"{label}\" แล้ว จะแจ้งเตือนเมื่อน้ำสูงขึ้น"])
    return Reply(text, [PROFILE_Q])


def _rescue(conn, uid: int, t: str) -> Reply:
    _remember(conn, uid, t)   # parks sensitive facts; triage still uses them for this ticket
    prof = tools.get_user_profile(conn, uid)
    mem = memory.merged_view(prof["memory"], prof["pending"])
    f = triage.extract(t, mem)
    place = find_place(t)
    loc = (place.lat, place.lon) if place else (
        (prof["locations"][0]["lat"], prof["locations"][0]["lon"]) if prof["locations"] else (None, None))
    tid = tools.create_rescue_ticket(conn, uid, t, loc[0], loc[1], f["people_count"], f["vulnerable"], f["needs"],
                                     f["urgency"], status="unconfirmed")
    lines = ["🚨 ถ้าอันตรายตอนนี้ โทร 1784 (ปภ.) หรือ 1669 (เจ็บป่วยฉุกเฉิน) ทันที • LINE @1784DDPM",
             "", f"น้ำมาเตรียมคำขอความช่วยเหลือ #{tid} ให้ทีมช่วยเหลือแล้ว:",
             f"• จำนวนคน: {f['people_count'] or 'ไม่ระบุ'}",
             f"• ผู้ที่ต้องดูแลพิเศษ: {', '.join(f['vulnerable']) or 'ไม่ระบุ'}",
             f"• ต้องการ: {', '.join(triage.NEEDS_TH.get(n, n) for n in f['needs'])}",
             f"• ความเร่งด่วน: {'วิกฤต' if f['urgency'] == 'critical' else 'สูง'}",
             f"• ตำแหน่ง: {'มีแล้ว' if loc[0] else 'ยังไม่มี กรุณาแชร์ตำแหน่ง'}",
             "", "ระหว่างรอ: ขึ้นที่สูง ปิดไฟฟ้า อยู่ห่างกระแสน้ำ เปิดโทรศัพท์ไว้"]
    quick = [_q("ยืนยันส่งคำขอ", f"ยืนยันขอความช่วยเหลือ #{tid}"), LOCATION_Q]
    return Reply("\n".join(lines), quick, ticket_id=tid)


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
