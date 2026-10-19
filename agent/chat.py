"""One turn of conversation, channel-agnostic. LINE and the web app both call handle().

Flow (CLAUDE.md §6, manual tool flow so it works with or without MaaS function calling):
classify intent in code → call tools → template facts (+ optional LLM advice) → reply.
"""
import re
import time
from dataclasses import dataclass, field

from agent import answer, memory, tools, triage
from agent.geo import PROVINCE_ALIASES, find_place, place_candidates
from agent.risk import rank

CONSENT_YES, CONSENT_NO = "ยินยอมให้จำข้อมูล", "ไม่ยินยอม"
FORGET_ALL_TEXT = "ลบข้อมูลทั้งหมด"  # same text as the profile quick reply (web postback memory:delete_all)
APPROX_NOTE = "ประมาณจากชื่อพื้นที่: "  # user_locations.notes of a place geocoded from its name, not a GPS share
CONSENT_PROMPT = ("🔒 ขออนุญาตจำข้อมูลนี้ เพื่อแจ้งเตือนและช่วยเหลือได้เร็วขึ้นไหม?\n"
                  "ข้อมูลสุขภาพเป็นข้อมูลอ่อนไหวตาม PDPA ถ้าไม่ยินยอม จะใช้เฉพาะในบทสนทนานี้")
_ABOUT = ("น้ำมาช่วยดูความเสี่ยงน้ำท่วมจากข้อมูลสถานีวัดน้ำจริง\n"
          "ลองพิมพ์ เช่น \"บ้านแม่อยู่บางบาล อยุธยา น้ำจะถึงไหม\" หรือกดแชร์ตำแหน่ง\n"
          "ถ้าอยู่ในอันตราย โทร 1784 (ปภ.) ได้ทันที\n\n" + answer.FOOTER)
HELP = "สวัสดีค่ะ " + _ABOUT                              # only for a greeting
NOT_UNDERSTOOD = "น้ำมายังไม่เข้าใจข้อความนี้ค่ะ " + _ABOUT   # anything else nothing matched: never answered with a greeting
GREETING_RE = re.compile(r"^(สวัสดี|หวัดดี|ดีค่ะ|ดีครับ|ดีจ้า|เริ่ม|ช่วยอะไรได้|ทำอะไรได้|คุณคือใคร|ใช้ยังไง|"
                         r"(hello|hi|hey|start|good ?(morning|afternoon|evening))\b|おはよう|こんにちは)", re.I)
THANKS_RE = re.compile(r"^(ขอบคุณ|ขอบใจ|thx|thanks|thank you|ลาก่อน|บาย|bye)", re.I)
THANKS = "ยินดีค่ะ ถ้าอยากให้เช็คอีก พิมพ์ชื่อพื้นที่หรือกดแชร์ตำแหน่งได้ตลอด\nถ้าอยู่ในอันตราย โทร 1784 (ปภ.) ได้ทันที"
# "Where do I go": shelters, evacuation centres. Checked after danger, so "ติดอยู่ ไปศูนย์พักพิงไม่ได้" is still danger.
SHELTER_RE = re.compile(r"ศูนย์พักพิง|ที่พักพิง|ศูนย์อพยพ|จุดอพยพ|อพยพไปที่ไหน|อพยพไปไหน|ไปพักที่ไหน|ที่หลบภัย|"
                        r"ไปนอนที่ไหน|ไปนอนไหน|ที่พักฉุกเฉิน|ที่พักชั่วคราว|หลบน้ำ|หนีไปไหน|หนีไปที่ไหน|ไปไหนดี|"
                        r"ไปที่ไหนดี|(มี)?ที่(ไหน)?รับ(สัตว์|คน|ผู้|เด็ก)|shelter|evacuation cent", re.I)
RISK_WORDS = ("น้ำ", "ท่วม", "ตลิ่ง", "อพยพ", "เสี่ยง", "ฝนตก", "ดินถล่ม", "ดินสไลด์", "ดินโคลน", "flood", "water level")
# Words with น้ำ/ฝน/ปลอดภัย… in them that are not about floods: removed before looking for a risk question.
NOT_FLOOD = re.compile(r"น้ำหอม|น้ำใจ|น้ำตา|น้ำผลไม้|น้ำมัน|น้ำปลา|น้ำแข็ง|หิวน้ำ|น้ำตก|อาบน้ำ|ห้องน้ำ|น้ำเสียง|"
                       r"น้ำหนัก|ท่อน้ำ|น้ำประปา|ตลาดน้ำ|น้ำท่วมปาก|น้ำชา|น้ำอัดลม|น้ำเปล่า|ดอยคำ|ไซเบอร์|"
                       r"ต่อสายตา|ข้อสอบ|หนัง|เกม|ระดับความ|ฝนดาวตก|ฝนตกพรำ")
# Clearly about something else ("ปลอดภัยไซเบอร์", "อันตรายต่อสายตา"): not a risk question unless a flood word is there too.
OFF_TOPIC = re.compile(r"ไซเบอร์|สายตา|สุขภาพจิต|ข้อสอบ|หนัง|เกม|ระดับความ|ออนไลน์|รหัสผ่าน|password|cyber", re.I)
FLOOD_WORDS = re.compile(r"น้ำ|ท่วม|ฝน|ดินถล่ม|ดินโคลน|อพยพ|flood")
# "Will I be OK?" with no flood word in it ("บ้านอยู่ตีนดอยสุเทพ จะเป็นอะไรไหม") is still a risk question.
CONCERN_RE = re.compile(r"เป็นอะไร(ไหม|มั้ย|มั๊ย|หรือเปล่า|รึเปล่า|ป่าว|ป่ะ)|เป็นไร(ไหม|มั้ย|มั๊ย)|เป็นหยัง|"
                        r"ปลอดภัย|อันตราย|น่าห่วง|น่ากลัว|ต้องย้าย|ต้องหนี|ต้องออกจาก|โดน(ไหม|มั้ย|ด้วย)|"
                        r"รอด(ไหม|มั้ย)|ถึง(ไหม|มั้ย)|is (it|my \w+) (ok|safe)|\bsafe\b", re.I)
# "เป็นไงบ้าง" is a risk question only about a place: "บ้านเป็นไงบ้าง", "แถวนี้เป็นยังไง".
HOW_IS_RE = re.compile(r"เป็นไง|เป็นยังไง|เป็นอย่างไร")
PLACE_WORDS = re.compile(r"บ้าน|แถวนี้|ที่นี่|ตรงนี้|ที่นั่น|ทางนี้|แถวบ้าน|ที่ทำงาน")
# Places people name besides homes; "ที่ทำงานจะท่วมไหม" with no saved ที่ทำงาน is asked about, not answered for บ้าน.
OTHER_PLACES = re.compile(r"ที่ทำงาน|ออฟฟิศ|โรงเรียน|คอนโด|หอพัก|โรงงาน")
# Asking for a promise of safety, or whether to evacuate: always answered with "follow official orders" (rule 3).
ASSURE_RE = re.compile(r"ปลอดภัย|รับรอง|แน่นอน|ไม่ท่วมแน่|อพยพ(ไหม|มั้ย)|ต้องอพยพ|ควรอพยพ|ไม่ต้องอพยพ|ต้องหนี|"
                       r"คำสั่งอพยพ|สั่งอพยพ|ย้ายของ|safe|evacuat", re.I)
ASSURE_NOTE = ("📢 น้ำมารับรองว่าปลอดภัยไม่ได้ ข้อมูลนี้เป็นการประเมินจากสถานีวัดน้ำเท่านั้น "
               "ถ้าทางการประกาศให้อพยพ ให้อพยพตามคำสั่งทันที สอบถามได้ที่ ปภ. 1784")
# Hillside places: river stations say nothing about flash floods or landslides there, so say so (rule 4).
HILL_RE = re.compile(r"ดอย|ภูเขา|เชิงเขา|ตีนเขา|ไหล่เขา|หุบเขา|ติดเขา|ใกล้เขา|บนเขา|ริมเขา|น้ำป่า|ดินถล่ม|"
                     r"ดินสไลด์|ดินโคลน|ห้วย")
HILL_NOTE = ("⛰️ สถานีวัดน้ำวัดระดับน้ำในแม่น้ำ ไม่ได้วัดน้ำป่าไหลหลากหรือดินถล่มบนเขา "
             "ถ้าฝนตกหนักต่อเนื่อง น้ำในลำห้วยขุ่นเป็นสีโคลน หรือได้ยินเสียงดังผิดปกติจากบนเขา "
             "ให้ออกจากริมห้วยและที่ลาดเชิงเขาทันที และติดตามประกาศของกรมอุตุนิยมวิทยาและ ปภ.")
CANCEL_RE = re.compile(r"^(ยกเลิก|ไม่เอา|ไม่ต้อง|cancel)", re.I)
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
    sources: list[dict] = field(default_factory=list)   # docs/api.md Source objects (used by the web API)
    emergency: bool = False                             # danger signalled: the web shows the 1784 card first
    place: dict | None = None                           # {label, lat, lon, approx, saved_label}: lets LINE show a map pin + card
    extra: list[str] = field(default_factory=list)      # notes already inside text that the LINE card does not repeat


def _q(label, text=None):
    return QuickReply(label[:20], text or label)


LOCATION_Q = QuickReply("แชร์ตำแหน่ง", kind="location")
PROFILE_Q = _q("ข้อมูลที่จำไว้", "ข้อมูลอะไรที่จำเกี่ยวกับฉัน")
SET_HOME = "ตั้งตำแหน่งบ้าน"            # profile quick replies: edit the home, add another place, add household facts
ADD_PLACE = "เพิ่มสถานที่"
ADD_FACTS = "เพิ่มข้อมูลครอบครัว"
AWAIT_TTL = 15 * 60                      # a half-finished "add a place" is forgotten after 15 minutes
HOME = "บ้าน"


def handle(conn, external_id: str | None, channel: str, text: str | None = None,
           location: tuple[float, float] | None = None, display_name: str | None = None,
           user_id: int | None = None) -> Reply:
    """external_id: the LINE userId, or 'web:<session>' for anonymous web chat. A signed-in web user passes
    user_id instead (their account may have no LINE id: Google sign-in)."""
    if user_id is not None:
        uid = user_id
        conn.execute("INSERT INTO user_memory (user_id) VALUES (%s) ON CONFLICT DO NOTHING", (uid,))
    else:
        uid = tools.get_or_create_user(conn, external_id, display_name)
    if text:
        tools.log_message(conn, uid, channel, "user", text)
    elif location:
        tools.log_message(conn, uid, channel, "user", f"[ตำแหน่ง {location[0]:.5f},{location[1]:.5f}]")
    reply = _route(conn, uid, text or "", location)
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
    return Reply(text, reply.quick, reply.sources, reply.emergency)


def _route(conn, uid: int, text: str, location) -> Reply:
    t = text.strip()
    if t == CONSENT_YES:
        facts = tools.pop_pending(conn, uid)
        if not facts:
            return Reply("ตอนนี้ไม่มีข้อมูลที่รอการยินยอมค่ะ ดูข้อมูลที่จำไว้ได้ที่ปุ่มด้านล่าง", [PROFILE_Q])
        tools.record_consent(conn, uid, True)
        tools.merge_memory(conn, uid, facts)
        lines = memory.describe(facts)
        return Reply("ขอบคุณค่ะ บันทึกแล้ว: " + (", ".join(lines) if lines else "-") +
                     "\nดูหรือลบได้ตลอด พิมพ์ \"ข้อมูลอะไรที่จำเกี่ยวกับฉัน\"", [PROFILE_Q])
    if t == CONSENT_NO:
        tools.record_consent(conn, uid, False)
        return Reply("ได้ค่ะ จะไม่บันทึกข้อมูลนี้ จะใช้เฉพาะในบทสนทนานี้เท่านั้น")
    if location:
        return _on_location(conn, uid, location)
    if t in (SET_HOME, ADD_PLACE, ADD_FACTS):
        return _start_edit(conn, uid, t)
    if (step := _awaiting(conn, uid)) and step["step"] == "name":
        if CANCEL_RE.match(t):
            _clear_await(conn, uid)
            return Reply("ยกเลิกการเพิ่มสถานที่แล้วค่ะ", [PROFILE_Q])
        if _place_name_ok(t):
            return _got_place_name(conn, uid, t)
        _clear_await(conn, uid)          # a question or a place sentence, not a name: answer it instead
    if t == HOME:
        return _home(conn, uid)
    if triage.is_danger(t):
        return _emergency(conn, uid, t)
    asks = _asks_risk(t)
    if t.startswith("ลบ") and not asks:
        return _forget(conn, uid, t)
    if re.search(r"จำอะไร|ข้อมูลอะไรที่จำ|ข้อมูลของฉัน|โปรไฟล์", t) and not asks:
        return _profile(conn, uid)
    if SHELTER_RE.search(t):
        return _shelters(conn, uid, t)
    return _talk(conn, uid, t)


def _place_name_ok(t: str) -> bool:
    """A name for the place being added ("บ้านแม่", "ร้าน"), not a question, a sentence or a place description."""
    return (0 < len(t) <= 20 and "?" not in t and not _asks_risk(t) and not triage.is_danger(t)
            and not find_place(t) and t not in (SET_HOME, ADD_PLACE, ADD_FACTS, CONSENT_YES, CONSENT_NO))


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
            extra: list[str] | None = None, saved: str | None = None) -> tuple[str, list[dict]]:
    """Reply text plus the sources (stations, shelter, GISTDA) whose numbers that text shows.
    saved: the label this place was just saved under. Alerts are promised only when a station is in range."""
    prof = tools.get_user_profile(conn, uid)
    mem = memory.merged_view(prof["memory"], prof["pending"])
    stations = tools.get_nearest_stations(conn, lat, lon, k=3)
    if saved:
        extra = [f"📌 บันทึก \"{saved}\" ไว้แล้ว " + ("ถ้าน้ำที่สถานีใกล้ๆ สูงขึ้น จะส่งข้อความเตือน" if stations
                 else "แต่ตอนนี้ยังไม่มีสถานีวัดน้ำใกล้พอที่จะแจ้งเตือนได้")] + list(extra or [])
    hh = mem.get("household") or {}
    shelters = tools.find_shelters(conn, lat, lon, {"patients": bool(hh.get("immobile")), "pets": bool(hh.get("pets"))})
    extent = tools.check_flood_extent(conn, lat, lon)
    recurrence = tools.get_flood_recurrence(lat, lon)
    text = answer.risk_answer(label, approx, stations, shelters, mem, extent, recurrence, extra)
    return text, _sources(stations, shelters, extent, recurrence, approx)


def _sources(stations: list[dict], shelters: list[dict], extent: dict | None, recurrence: dict | None,
             approx: bool) -> list[dict]:
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
        out.append({"type": "gistda", "kind": "flood_extent", "observed_at": extent["as_of"],
                    "summary": answer.flood_extent_text(extent, approx)})
    if recurrence is not None and (summary := answer.recurrence_text(recurrence)):
        out.append({"type": "gistda", "kind": "flood_recurrence", "observed_at": None, "summary": summary})
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


def _asks_risk(t: str) -> bool:
    if OFF_TOPIC.search(t) and not FLOOD_WORDS.search(NOT_FLOOD.sub(" ", t)):
        return False
    t = NOT_FLOOD.sub(" ", t).lower()
    return (any(w in t for w in RISK_WORDS) or bool(CONCERN_RE.search(t.replace(" ", "")) or CONCERN_RE.search(t))
            or bool(HOW_IS_RE.search(t) and PLACE_WORDS.search(t)))


def _saved_named(t: str, locations: list[dict]) -> dict | None:
    """The saved place whose label the text names, longest label first ("บ้านแม่" before "บ้าน")."""
    named = [l for l in locations if l["label"] in t]
    return max(named, key=lambda l: len(l["label"])) if named else None


def _mostly_place(t: str, place) -> bool:
    """"บางบาล อยุธยา" alone is a request to check it; "ไปเที่ยวบ้านโพธิ์ไหม" is not."""
    rest = t.replace(" ", "")
    for name in (place.tambon, place.amphoe, place.province, "ตำบล", "อำเภอ", "จังหวัด", "ต.", "อ.", "จ.",
                 *(a for a, v in PROVINCE_ALIASES.items() if v == place.province)):
        if name:
            rest = rest.replace(name, "")
    return len(rest) <= 4


def _talk(conn, uid: int, t: str) -> Reply:
    facts, need_consent = _remember(conn, uid, t)
    prof = tools.get_user_profile(conn, uid)
    saved_labels = tuple(l["label"] for l in prof["locations"])
    place = find_place(t)
    label = memory.location_label(t)
    asks = _asks_risk(t)
    hill = bool(HILL_RE.search(NOT_FLOOD.sub(" ", t).replace(" ", "")))
    assure = bool(ASSURE_RE.search(t))
    if place and not (asks or label or _mostly_place(t, place)):
        place = None                     # a place mentioned in passing, not a question about it
    if place and not place.amphoe:
        return _ask_place(label, [], hill, assure, need_consent, province=place.province)
    cands = [] if place else place_candidates(t, skip=saved_labels)
    named = _saved_named(t, prof["locations"])
    other = OTHER_PLACES.search(t)
    # The message names a place we could not pin down ("บ้านอยู่ตีนดอยสุเทพ", "ที่ทำงาน" not saved): ask which one,
    # never answer for a different saved place instead.
    unresolved = not place and bool(cands or (label and (label not in saved_labels or "อยู่" in t))
                                    or (other and not (named and other.group(0) in named["label"])))
    target = None
    if place:
        target = (place.lat, place.lon, (label + " " if label else "") + place.label, True)
        if label:
            tools.save_location(conn, uid, label, place.lat, place.lon, notes=APPROX_NOTE + place.label)
    elif prof["locations"] and asks and not unresolved:
        loc = named or prof["locations"][0]
        target = (loc["lat"], loc["lon"], loc["label"], _approx(loc))

    if target:
        extra = []
        if assure:
            extra.append(ASSURE_NOTE)
        if hill:
            extra.append(HILL_NOTE)
        if not prof["memory"].get("household") and not prof["pending"]:
            extra.append("👪 ที่บ้านมีผู้สูงอายุ คนที่เดินไม่ได้ เด็กเล็ก หรือสัตว์เลี้ยงไหม? บอกได้ จะช่วยวางแผนให้เหมาะกับครอบครัว")
        quick = [LOCATION_Q, PROFILE_Q]
        if need_consent:
            extra.append(CONSENT_PROMPT)
            quick = _consent_quick()
        saved = label if label and place else None
        text, sources = _assess(conn, uid, target[0], target[1], target[2], target[3], extra, saved=saved)
        place_info = {"label": target[2], "lat": target[0], "lon": target[1], "approx": target[3],
                      "saved_label": saved}
        return Reply(text, quick, sources=sources, place=place_info, extra=extra)

    if facts and not asks:
        lines = memory.describe(facts)
        if need_consent:
            return Reply("รับทราบค่ะ: " + ", ".join(lines) + "\n\n" + CONSENT_PROMPT, _consent_quick())
        return Reply("รับทราบและบันทึกแล้วค่ะ: " + ", ".join(lines), [PROFILE_Q])
    if unresolved or asks:
        reply = _ask_place(label, cands, hill, assure, need_consent)
        if facts and (lines := memory.describe(facts)) and not need_consent:
            reply.text = "รับทราบและบันทึกแล้วค่ะ: " + ", ".join(lines) + "\n\n" + reply.text
        return reply
    if GREETING_RE.match(t):
        return Reply(HELP, [LOCATION_Q, _q("บ้านอยู่บางบาล อยุธยา น้ำจะถึงไหม")])
    if THANKS_RE.match(t):
        return Reply(THANKS, [LOCATION_Q])
    return Reply(NOT_UNDERSTOOD, [LOCATION_Q, _q("บ้านอยู่บางบาล อยุธยา น้ำจะถึงไหม")])


def _ask_place(label: str | None, cands: list, hill: bool, assure: bool, need_consent: bool,
               province: str | None = None) -> Reply:
    """A risk question we cannot place yet. Offer the subdistricts it names, else ask for the district."""
    if province:
        lines = [f"จ.{province} กว้างเกินกว่าจะประเมินจากสถานีวัดน้ำได้ค่ะ บอกอำเภอหรือตำบลด้วย "
                 f"(เช่น \"บ้านอยู่ ต.... อ.... จ.{province}\") หรือกดแชร์ตำแหน่ง"]
        quick = [LOCATION_Q]
    elif cands:
        lines = ["หมายถึงพื้นที่ไหนคะ? กดเลือกด้านล่าง หรือพิมพ์ชื่อตำบล อำเภอ จังหวัดให้ครบ"]
        lines += [f"• {p.label}" for p in cands]
        quick = [_q(f"ต.{p.tambon} {p.province}", f"{label + 'อยู่ ' if label else ''}{p.label} น้ำจะถึงไหม")
                 for p in cands] + [LOCATION_Q]
    else:
        lines = ["ยังหาตำแหน่งนี้ไม่เจอค่ะ บอกชื่อตำบล อำเภอ และจังหวัด (เช่น \"บ้านอยู่บางบาล อยุธยา\") "
                 "หรือกดแชร์ตำแหน่ง แล้วจะเช็คสถานีวัดน้ำใกล้ที่สุดให้"]
        quick = [LOCATION_Q]
    if assure:
        lines += ["", ASSURE_NOTE]
    if hill:
        lines += ["", HILL_NOTE]
    lines += ["", "ถ้าอยู่ในอันตรายตอนนี้ โทร 1784 (ปภ.) ได้ทันที"]
    if need_consent:
        lines += ["", CONSENT_PROMPT]
        quick = _consent_quick()
    return Reply("\n".join(lines), quick)


def _shelters(conn, uid: int, t: str) -> Reply:
    """Shelters near a named place, else near a saved place (the one named in the text, else the first)."""
    _, need_consent = _remember(conn, uid, t)
    prof = tools.get_user_profile(conn, uid)
    place = find_place(t)
    if place and not place.amphoe:
        return Reply(f"จ.{place.province} กว้างเกินไปค่ะ บอกอำเภอหรือตำบล หรือกดแชร์ตำแหน่ง "
                     "แล้วจะหาศูนย์พักพิงที่ใกล้ที่สุดให้\nถ้าต้องการความช่วยเหลือด่วน โทร 1784 ได้ทันที", [LOCATION_Q])
    if place:
        lat, lon, label, approx = place.lat, place.lon, place.label, True
    elif prof["locations"]:
        loc = _saved_named(t, prof["locations"]) or prof["locations"][0]
        lat, lon, label = loc["lat"], loc["lon"], loc["label"]
        approx = _approx(loc)
    else:
        return Reply("บอกชื่อตำบล/อำเภอ หรือกดแชร์ตำแหน่ง แล้วจะหาศูนย์พักพิงที่ใกล้ที่สุดให้ค่ะ\n"
                     "ถ้าต้องการความช่วยเหลือด่วน โทร 1784 ได้ทันที", [LOCATION_Q])
    mem = memory.merged_view(prof["memory"], prof["pending"])
    hh = mem.get("household") or {}
    need_patients = bool(hh.get("immobile"))
    shelters = tools.find_shelters(conn, lat, lon, {"patients": need_patients, "pets": bool(hh.get("pets"))}, k=3)
    text = answer.shelter_answer(label, approx, shelters, need_patients)
    quick = [LOCATION_Q, PROFILE_Q]
    if need_consent:
        text += "\n\n" + CONSENT_PROMPT
        quick = _consent_quick()
    sources = [{"type": "shelter", "shelter_id": sh["id"], "name": sh["name"], "distance_km": sh["distance_km"]}
               for sh in shelters]
    return Reply(text, quick, sources=sources)


def _on_location(conn, uid: int, location) -> Reply:
    lat, lon = location
    label = _save_shared(conn, uid, lat, lon)
    text, sources = _assess(conn, uid, lat, lon, label, approx=False, saved=label)
    return Reply(text, [PROFILE_Q], sources=sources,
                 place={"label": label, "lat": lat, "lon": lon, "approx": False, "saved_label": label})


def _awaiting(conn, uid: int) -> dict | None:
    """The half-finished profile edit ({step: 'name'|'loc', label}), if it is still fresh."""
    a = (tools.get_user_profile(conn, uid)["memory"].get("preferences") or {}).get("_await")
    return a if a and time.time() - a.get("ts", 0) < AWAIT_TTL else None


def _set_await(conn, uid: int, step: str, label: str | None = None) -> None:
    tools.merge_memory(conn, uid, {"preferences": {"_await": {"step": step, "label": label, "ts": time.time()}}})


def _clear_await(conn, uid: int) -> None:
    conn.execute("UPDATE user_memory SET preferences = preferences - '_await' WHERE user_id = %s", (uid,))


def _save_shared(conn, uid: int, lat: float, lon: float) -> str:
    """Save a shared GPS fix and return the label it was saved under: the name being added or edited if there is
    one, else "บ้าน" when there is no home yet, else "ตำแหน่งที่แชร์"."""
    a = _awaiting(conn, uid)
    prof = tools.get_user_profile(conn, uid)
    if a and a["step"] == "loc" and a.get("label"):
        label = a["label"]
    elif not any(l["label"] == HOME for l in prof["locations"]):
        label = HOME
    else:
        label = "ตำแหน่งที่แชร์"
    tools.save_location(conn, uid, label, lat, lon)
    _clear_await(conn, uid)
    return label


def _start_edit(conn, uid: int, t: str) -> Reply:
    if t == SET_HOME:
        _set_await(conn, uid, "loc", HOME)
        return Reply("📍 กดแชร์ตำแหน่งที่บ้านใหม่ได้เลยค่ะ น้ำมาจะบันทึกเป็น \"บ้าน\" แทนที่ตำแหน่งเดิม\n"
                     "หรือพิมพ์ชื่อพื้นที่ เช่น \"บ้านอยู่บางบาล อยุธยา\"", [LOCATION_Q])
    if t == ADD_PLACE:
        _set_await(conn, uid, "name")
        return Reply("ตั้งชื่อสถานที่ว่าอะไรดีคะ เช่น บ้านแม่ ที่ทำงาน ร้านค้า (พิมพ์ชื่อได้เลย)")
    return Reply("บอกได้เลยค่ะ น้ำมาจะบันทึกให้ เช่น\n"
                 "• \"ที่บ้านมีผู้สูงอายุ\" • \"มีสุนัข 2 ตัว\" • \"บ้านสองชั้น ใต้ถุนสูง\" • \"มีรถยนต์\"\n"
                 "ข้อมูลสุขภาพ (เช่น ติดเตียง ฟอกไต) จะขออนุญาตก่อนบันทึกทุกครั้ง\n"
                 "ถ้าต้องการแก้ข้อมูลเดิม ให้กด \"ลบ ...\" แล้วบอกใหม่ค่ะ", [PROFILE_Q])


def _got_place_name(conn, uid: int, name: str) -> Reply:
    _set_await(conn, uid, "loc", name)
    return Reply(f"📍 กดแชร์ตำแหน่งของ \"{name}\" ได้เลยค่ะ", [LOCATION_Q])


def _home(conn, uid: int) -> Reply:
    """A bare "บ้าน": assess the saved home, or start saving one."""
    prof = tools.get_user_profile(conn, uid)
    loc = next((l for l in prof["locations"] if l["label"] == HOME), None)
    if not loc:
        _set_await(conn, uid, "loc", HOME)
        return Reply("ยังไม่ได้บันทึก \"บ้าน\" ค่ะ กดแชร์ตำแหน่งที่บ้านได้เลย "
                     "น้ำมาจะบันทึกไว้และแจ้งเตือนเมื่อน้ำสูงขึ้น", [LOCATION_Q])
    approx = bool(loc["notes"] and loc["notes"].startswith("ประมาณ"))
    text, sources = _assess(conn, uid, loc["lat"], loc["lon"], HOME, approx, [])
    return Reply(text, [LOCATION_Q, PROFILE_Q], sources=sources,
                 place={"label": HOME, "lat": loc["lat"], "lon": loc["lon"], "approx": approx, "saved_label": None})


def _approx(loc: dict) -> bool:
    """A saved place geocoded from its name (a district centre), not a GPS share."""
    return bool(loc["notes"] and loc["notes"].startswith(APPROX_NOTE))


def _place_words(t: str, locations: list[dict]) -> str | None:
    """Where the person is, in words 1784 can use: a place named in the message, else the first saved place
    (its district, or its coordinates when it was a GPS share)."""
    if place := find_place(t):
        return place.label + " (บอกบ้านเลขที่ หมู่บ้าน หรือจุดสังเกตเพิ่มด้วย)"
    if not locations:
        return None
    loc = locations[0]
    if _approx(loc):
        return (f"{loc['label']} ({loc['notes'].removeprefix(APPROX_NOTE)}) เป็นตำแหน่งโดยประมาณ "
                "บอกบ้านเลขที่ หมู่บ้าน หรือจุดสังเกตเพิ่มด้วย")
    return f"{loc['label']} (พิกัด {loc['lat']:.5f}, {loc['lon']:.5f})"


def _emergency(conn, uid: int, t: str) -> Reply:
    """Danger: 1784 first, then what to tell them. NamMa sends nothing on the person's behalf, and says so."""
    _clear_await(conn, uid)   # a half-finished "add a place" must not swallow the next location share
    _remember(conn, uid, t)   # parks sensitive facts; the call details below still use them, for this reply only
    prof = tools.get_user_profile(conn, uid)
    d = triage.call_details(t, memory.merged_view(prof["memory"], prof["pending"]))
    tell = []
    if where := _place_words(t, prof["locations"]):
        tell.append(f"• ตำแหน่ง: {where}")
    if d["vulnerable"]:
        tell.append(f"• ผู้ที่ต้องช่วยเป็นพิเศษ: {', '.join(d['vulnerable'])}")
    if d["people_count"]:
        tell.append(f"• จำนวนคน: {d['people_count']}")
    lines = [RESCUE_HOTLINE, "", "น้ำมาไม่ได้ส่งเรื่องให้หน่วยกู้ภัยแทน ต้องโทร 1784 แจ้งเอง"]
    if tell:
        lines += ["ตอนโทร แจ้งว่า:", *tell]
    lines += ["", "ระหว่างรอ:",
              "• ขึ้นไปชั้นบนหรือที่สูง พาผู้ที่เดินไม่ได้ขึ้นก่อน",
              "• ตัดไฟชั้นล่างถ้าทำได้โดยไม่ต้องลุยน้ำ",
              "• เตรียมยา น้ำดื่ม ไฟฉาย และเปิดโทรศัพท์ไว้",
              "• ถ้าสถานการณ์แย่ลง โทร 1784 ซ้ำได้ทันที"]
    return Reply("\n".join(lines), emergency=True)


def _profile(conn, uid: int) -> Reply:
    prof = tools.get_user_profile(conn, uid)
    lines = memory.describe(prof["memory"])
    locs = [f"📍 {l['label']}" for l in prof["locations"]]
    if not lines and not locs:
        head = "ตอนนี้ยังไม่ได้จำข้อมูลอะไรเกี่ยวกับคุณค่ะ"
    else:
        head = "ข้อมูลที่น้ำมาจำไว้:\n" + "\n".join(locs + [f"• {x}" for x in lines])
    text = head + "\n\nเพิ่มหรือแก้ไขได้ค่ะ: กดปุ่มด้านล่าง หรือพิมพ์ เช่น \"บ้านแม่อยู่บางระกำ พิษณุโลก\""
    quick = [_q("📍 ตั้งตำแหน่งบ้าน", SET_HOME), _q("➕ เพิ่มสถานที่", ADD_PLACE), _q("➕ ข้อมูลครอบครัว", ADD_FACTS)]
    quick += [_q(f"ลบ {l['label']}") for l in prof["locations"][:4]]
    if prof["memory"].get("household") or prof["memory"].get("needs"):
        quick.append(_q("ลบข้อมูลสุขภาพ"))
    if lines or locs:
        quick.append(_q("ลบข้อมูลทั้งหมด"))
    return Reply(text, quick[:13])


def _forget(conn, uid: int, t: str) -> Reply:
    target = " ".join(t[2:].split())
    if not target:
        return Reply("จะลบข้อมูลไหนคะ? ดูข้อมูลที่จำไว้และกดลบได้ที่ปุ่มด้านล่าง", [PROFILE_Q])
    if target in ("ข้อมูลของฉัน", "ข้อมูล"):
        return _profile(conn, uid)
    if target in ("ข้อมูลทั้งหมด", "ทั้งหมด"):
        tools.forget(conn, uid, "all")
        return Reply("ลบข้อมูลทั้งหมดแล้วค่ะ")
    if target == "ข้อมูลสุขภาพ":
        prof = tools.get_user_profile(conn, uid)
        if not (prof["memory"].get("household") or prof["memory"].get("needs") or prof["pending"]):
            return Reply("ไม่มีข้อมูลสุขภาพที่บันทึกไว้ค่ะ", [PROFILE_Q])
        tools.forget(conn, uid, "household")
        tools.forget(conn, uid, "needs")
        return Reply("ลบข้อมูลสุขภาพและสมาชิกในบ้านแล้วค่ะ")
    for loc in tools.get_user_profile(conn, uid)["locations"]:
        if loc["label"] == target:
            tools.forget(conn, uid, f"location:{loc['id']}")
            return Reply(f"ลบ \"{target}\" แล้ว จะไม่แจ้งเตือนสำหรับที่นี่อีก")
    return Reply("ไม่พบข้อมูลนั้นค่ะ", [PROFILE_Q])
