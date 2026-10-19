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
FORGET_ALL_TEXT = "ลบข้อมูลทั้งหมด"  # same text as the profile quick reply (web postback memory:delete_all)
APPROX_NOTE = "ประมาณจากชื่อพื้นที่: "  # user_locations.notes of a place geocoded from its name, not a GPS share
CONSENT_PROMPT = ("🔒 ขออนุญาตจำข้อมูลนี้ เพื่อแจ้งเตือนและช่วยเหลือได้เร็วขึ้นไหม?\n"
                  "ข้อมูลสุขภาพเป็นข้อมูลอ่อนไหวตาม PDPA ถ้าไม่ยินยอม จะใช้เฉพาะในบทสนทนานี้")
HELP = ("สวัสดีค่ะ น้ำมาช่วยดูความเสี่ยงน้ำท่วมจากข้อมูลสถานีวัดน้ำจริง\n"
        "ลองพิมพ์ เช่น \"บ้านแม่อยู่บางบาล อยุธยา น้ำจะถึงไหม\" หรือกดแชร์ตำแหน่ง\n"
        "ถ้าอยู่ในอันตราย โทร 1784 (ปภ.) ได้ทันที\n\n" + answer.FOOTER)
# "Where do I go": shelters, evacuation centres. Checked after danger, so "ติดอยู่ ไปศูนย์พักพิงไม่ได้" is still danger.
SHELTER_RE = re.compile(r"ศูนย์พักพิง|ที่พักพิง|ศูนย์อพยพ|จุดอพยพ|อพยพไปที่ไหน|อพยพไปไหน|ไปพักที่ไหน|ที่หลบภัย")
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
    sources: list[dict] = field(default_factory=list)   # docs/api.md Source objects (used by the web API)
    emergency: bool = False                             # danger signalled: the web shows the 1784 card first


def _q(label, text=None):
    return QuickReply(label[:20], text or label)


LOCATION_Q = QuickReply("แชร์ตำแหน่ง", kind="location")
PROFILE_Q = _q("ข้อมูลที่จำไว้", "ข้อมูลอะไรที่จำเกี่ยวกับฉัน")


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
        tools.record_consent(conn, uid, True)
        facts = tools.pop_pending(conn, uid)
        tools.merge_memory(conn, uid, facts)
        lines = memory.describe(facts)
        return Reply("ขอบคุณค่ะ บันทึกแล้ว: " + (", ".join(lines) if lines else "-") +
                     "\nดูหรือลบได้ตลอด พิมพ์ \"ข้อมูลอะไรที่จำเกี่ยวกับฉัน\"", [PROFILE_Q])
    if t == CONSENT_NO:
        tools.record_consent(conn, uid, False)
        return Reply("ได้ค่ะ จะไม่บันทึกข้อมูลนี้ จะใช้เฉพาะในบทสนทนานี้เท่านั้น")
    if location:
        return _on_location(conn, uid, location)
    if triage.is_danger(t):
        return _emergency(conn, uid, t)
    if re.search(r"จำอะไร|ข้อมูลอะไรที่จำ|ข้อมูลของฉัน|โปรไฟล์", t):
        return _profile(conn, uid)
    if t.startswith("ลบ"):
        return _forget(conn, uid, t)
    if SHELTER_RE.search(t):
        return _shelters(conn, uid, t)
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


def _talk(conn, uid: int, t: str) -> Reply:
    facts, need_consent = _remember(conn, uid, t)
    place = find_place(t)
    label = memory.location_label(t)
    prof = tools.get_user_profile(conn, uid)
    target = None
    if place:
        target = (place.lat, place.lon, (label + " " if label else "") + place.label, True)
        if label:
            tools.save_location(conn, uid, label, place.lat, place.lon, notes=APPROX_NOTE + place.label)
    elif prof["locations"] and any(w in t for w in RISK_WORDS):
        loc = next((l for l in prof["locations"] if l["label"] in t), prof["locations"][0])
        target = (loc["lat"], loc["lon"], loc["label"], _approx(loc))

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


def _shelters(conn, uid: int, t: str) -> Reply:
    """Shelters near a named place, else near a saved place (the one named in the text, else the first)."""
    _, need_consent = _remember(conn, uid, t)
    prof = tools.get_user_profile(conn, uid)
    place = find_place(t)
    if place:
        lat, lon, label, approx = place.lat, place.lon, place.label, True
    elif prof["locations"]:
        loc = next((l for l in prof["locations"] if l["label"] in t), prof["locations"][0])
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
    prof = tools.get_user_profile(conn, uid)
    label = "บ้าน" if not prof["locations"] else "ตำแหน่งที่แชร์"
    tools.save_location(conn, uid, label, lat, lon)
    text, sources = _assess(conn, uid, lat, lon, label, approx=False,
                            extra=[f"📌 บันทึกตำแหน่งนี้เป็น \"{label}\" แล้ว จะแจ้งเตือนเมื่อน้ำสูงขึ้น"])
    return Reply(text, [PROFILE_Q], sources=sources)


def _approx(loc: dict) -> bool:
    """A saved place geocoded from its name (a district centre), not a GPS share."""
    return bool(loc["notes"] and loc["notes"].startswith(APPROX_NOTE))


def _place_words(t: str, locations: list[dict]) -> str | None:
    """Where the person is, in words 1784 can use: a place named in the message, else the first saved place
    (its district, or its coordinates when it was a GPS share)."""
    if place := find_place(t):
        return place.label
    if not locations:
        return None
    loc = locations[0]
    if _approx(loc):
        return f"{loc['label']} ({loc['notes'].removeprefix(APPROX_NOTE)})"
    return f"{loc['label']} (พิกัด {loc['lat']:.5f}, {loc['lon']:.5f})"


def _emergency(conn, uid: int, t: str) -> Reply:
    """Danger: 1784 first, then what to tell them. NamMa sends nothing on the person's behalf, and says so."""
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
