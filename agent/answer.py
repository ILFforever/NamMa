"""Thai answer text. Facts lines are templated from tool output (rule 1: numbers only from tools);
the LLM, when configured, may only reword the opening answer and the advice paragraph, and is rejected if it adds
numbers.

The web app draws stations and shelters as cards from the sources, so risk_answer and shelter_answer also return
web swaps: (block of the text, shorter web text). chat.web_reply applies them; LINE gets the full text.
"""
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo

from agent import llm
from agent.risk import TIER_TH, rank

FOOTER = ("ℹ️ น้ำมาเป็นผู้ช่วยไม่เป็นทางการ โปรดติดตามประกาศและทำตามคำสั่งอพยพของทางการเสมอ\n"
          "☎️ สายด่วน ปภ. 1784 • LINE @1784DDPM")
FAR_STATION_KM = 20     # beyond this, say the station may not reflect the place
TIER_ICON = {"normal": "🟢", "watch": "🟡", "warning": "🟠", "danger": "🔴"}
STATION_NOT_HOUSE = "⚠️ ค่านี้วัดที่สถานีริมแม่น้ำ ไม่ใช่ระดับน้ำที่บ้านโดยตรง ใช้เป็นการประเมินเท่านั้น"
EVAC_ORDER_NOTE = "ถ้ามีคำสั่งอพยพจากทางการ ให้ไปตามจุดที่ทางการกำหนด"

BKK = ZoneInfo("Asia/Bangkok")

WebSwap = tuple[str, str]

ADVICE_SYSTEM = """คุณคือ "น้ำมา" ผู้ช่วยเรื่องน้ำท่วม พูดภาษาไทย สุภาพ สงบ ตรงประเด็น ใช้ "ค่ะ" ไม่ใช้ "ครับ"
เขียนคำแนะนำ "สิ่งที่ควรทำตอนนี้" 2-4 ข้อ สำหรับครอบครัวนี้ จากข้อมูลที่ให้เท่านั้น
บรรทัดละหนึ่งข้อ ข้อละหนึ่งประโยคสั้นๆ ไม่ต้องทักทาย ไม่ต้องเกริ่นนำหรือสรุป ไม่ใช้ markdown เช่น ** หรือ #
ห้ามใส่ตัวเลขใดๆ ที่ไม่มีในข้อมูล ห้ามบอกว่าปลอดภัยแน่นอน ห้ามบอกให้เพิกเฉยต่อคำสั่งอพยพของทางการ"""


def display_code(code: str | None) -> str:
    """ThaiWater codes as people know them: "ridhydro_C.36" → "C.36"."""
    return re.sub(r"^[a-z]+_", "", code or "")


def _ts(s: str | None) -> str:
    """"17:00 น. 19/10" in Bangkok time. reading_ts is written in the risk job's DB session zone (+08:00 on the live
    server), so the wall clock must be converted, or the text reads an hour off the web card for the same reading."""
    if not s:
        return "-"
    d = datetime.fromisoformat(s)
    d = d.replace(tzinfo=BKK) if d.tzinfo is None else d.astimezone(BKK)
    return f"{d:%H:%M} น. {d.day}/{d.month}"


def station_lines(s: dict) -> list[str]:
    river = f" ({s['river']})" if s.get("river") else ""
    lines = [f"📡 สถานีวัดน้ำ {display_code(s['code'])} {s['name_th']}{river} ห่าง {s['distance_km']:.1f} กม."]
    lvl = f"ระดับน้ำ {s['level_msl']:.2f} ม.รทก."
    d = s.get("diff_wl_bank")
    if d is not None:
        lvl += f" — {'สูงกว่าตลิ่ง' if d >= 0 else 'ต่ำกว่าตลิ่ง'} {abs(d):.2f} ม."
    lines.append("• " + lvl)
    t = s.get("trend_3h_cm_per_hr")
    if t is None:
        lines.append("• แนวโน้ม: ยังมีข้อมูลไม่พอ")
    elif t >= 0.5:
        lines.append(f"• 3 ชม.ล่าสุด: น้ำขึ้น {t:.1f} ซม./ชม.")
    elif t <= -0.5:
        lines.append(f"• 3 ชม.ล่าสุด: น้ำลง {abs(t):.1f} ซม./ชม.")
    else:
        lines.append("• 3 ชม.ล่าสุด: ทรงตัว")
    if forecast := _forecast(s):
        lines.append("• แบบจำลองคาดการณ์: " + forecast)
    if s.get("hours_to_bank") is not None:
        lines.append(f"• ถ้าขึ้นต่อในอัตรานี้ จะถึงระดับตลิ่งในราว {s['hours_to_bank']:.0f} ชม. (ประมาณการ)")
    for u in s.get("upstream") or []:
        if u.get("risk_tier") == "danger":
            eta = f" น้ำใช้เวลาราว {u['approx_travel_hours']:.0f} ชม. มาถึง" if u.get("approx_travel_hours") else ""
            lines.append(f"• ต้นน้ำ {display_code(u['oldcode'])} {u['name_th']} ล้นตลิ่งแล้ว{eta}")
    lines.append(f"{TIER_ICON[s['risk_tier']]} ระดับความเสี่ยงที่สถานี: {TIER_TH[s['risk_tier']]}")
    lines.append(f"ข้อมูล ณ {_ts(s.get('reading_ts'))} จาก ThaiWater (สสน.) / {s['agency'] or '-'}")
    return lines


def _forecast(s: dict) -> str | None:
    """"+12 ชม. … · +24 ชม. …" from the station's model scores, or None. The web cards don't show these."""
    m = s.get("model") or {}
    parts = []
    for h in ("12", "24"):
        p = m.get(h)
        if p and p.get("diff_wl_bank") is not None:
            d = p["diff_wl_bank"]
            parts.append(f"+{h} ชม. {'สูงกว่าตลิ่ง' if d >= 0 else 'ต่ำกว่าตลิ่ง'} {abs(d):.2f} ม. (±{p['mae_cm']:.0f} ซม.)")
        elif p:
            parts.append(f"+{h} ชม. ระดับ {p['level']:.2f} ม.รทก. (±{p['mae_cm']:.0f} ซม.)")
    return " · ".join(parts) or None


def verdict(s: dict) -> str:
    """One line for the web, above the station cards: the state at the station that decides the risk. No numbers."""
    d, t = s.get("diff_wl_bank"), s.get("trend_3h_cm_per_hr")
    if d is not None and d >= 0:
        state = "น้ำล้นตลิ่งแล้ว"
    elif t is not None and t >= 0.5:
        state = "น้ำกำลังขึ้น"
    else:
        state = "น้ำยังต่ำกว่าตลิ่ง"
    name = s["name_th"] if s["name_th"].startswith("สถานี") else "สถานี" + s["name_th"]
    return f"{name}: {state} · ความเสี่ยงระดับ{TIER_TH[s['risk_tier']]}"


def _rai(km2: float) -> str:
    rai = km2 * 625
    return "ไม่ถึง 1" if rai < 1 else f"{rai:,.0f}"


def flood_extent_text(extent: dict, approx: bool) -> str:
    """One line from agent.gistda.check_flood_extent. A place found by its name is only a district centre, so there
    it gives the flooded area around, not a yes/no for the point."""
    r = f"{extent['radius_km']:.0f} กม."
    if extent["nearest_km"] is None and (e := extent.get("earlier")):
        body = (f"ไม่พบพื้นที่น้ำท่วมในรัศมี {r} แต่ในรอบ {e['days']} วันเคยพบน้ำท่วมราว {_rai(e['area_km2_nearby'])} ไร่ "
                f"ห่างราว {e['nearest_km']:.1f} กม. น้ำอาจลดลงแล้ว")
    elif extent["nearest_km"] is None:
        body = f"ไม่พบพื้นที่น้ำท่วมในรัศมี {r}"
    elif approx:
        body = f"พื้นที่น้ำท่วมในรัศมี {r} รวมราว {_rai(extent['area_km2_nearby'])} ไร่"
    elif extent["at_point"]:
        body = "พบน้ำท่วมที่จุดนี้"
    else:
        body = f"ไม่พบน้ำท่วมที่จุดนี้ พื้นที่น้ำท่วมใกล้สุดห่างราว {extent['nearest_km']:.1f} กม."
    return f"ภาพดาวเทียม GISTDA {extent['days']} วันล่าสุด: {body} (ข้อมูล ณ {_ts(extent['as_of'])})"


def recurrence_text(rec: dict, approx: bool = False) -> str | None:
    """One line from agent.gistda.get_flood_recurrence: the years the subdistrict flooded, 2011–2024, and at a GPS
    point (not a place found by name, which is only a district centre) whether the point itself did."""
    years = rec.get("years") or []
    if not years:
        return None
    where = f"ต.{rec['tambon']}" if rec.get("tambon") else "พื้นที่นี้"
    line = f"สถิติ GISTDA 2011–2024: {where} มีน้ำท่วม {len(years)} ปี (ล่าสุดปี {max(years)})"
    if approx or "at_point" not in rec:
        return line
    here = rec["at_point"]
    return line + (f" จุดนี้เองท่วม {len(here)} ปี" if here else " ไม่พบน้ำท่วมที่จุดนี้เอง")


def rule_advice(tier: str, memory: dict) -> list[str]:
    hh, needs, house, assets = (memory.get(k) or {} for k in ("household", "needs", "house", "assets"))
    out = {
        "danger": ["น้ำล้นตลิ่งที่สถานีแล้ว ถ้าบ้านอยู่ที่ลุ่มหรือริมน้ำ ให้ย้ายคนและของสำคัญไปที่สูงหรือศูนย์พักพิงตอนนี้",
                   "ทำตามประกาศอพยพของทางการทันที ปิดเบรกเกอร์ไฟก่อนน้ำเข้าบ้าน"],
        "warning": ["เตรียมพร้อมอพยพ: ยกของขึ้นที่สูง เตรียมยา เอกสารสำคัญ ไฟฉาย น้ำดื่ม และชาร์จโทรศัพท์",
                    "ตกลงกันในบ้านว่าจะไปที่ไหน และไปอย่างไร ถ้าต้องออกจากบ้าน"],
        "watch": ["ติดตามระดับน้ำใกล้ชิด ยกของสำคัญขึ้นที่สูงไว้ก่อน", "เตรียมกระเป๋าฉุกเฉินให้พร้อม"],
        "normal": ["ระดับน้ำที่สถานียังต่ำกว่าตลิ่ง แต่สถานการณ์เปลี่ยนเร็ว ติดตามประกาศของทางการต่อเนื่อง"],
    }[tier]
    urgent = rank(tier) >= rank("warning")
    if hh.get("immobile"):
        who = ", ".join(hh["immobile"])
        out.insert(0, f"มี{who}ที่เดินไม่ได้: ควรย้ายไปที่ปลอดภัยก่อนน้ำมา ไม่ควรรอ และประสานรถหรือเรือไว้ล่วงหน้า"
                   if urgent else f"มี{who}ที่เดินไม่ได้: วางแผนการย้ายและติดต่อคนช่วยไว้ล่วงหน้า")
    if needs.get("dialysis"):
        out.append("ติดต่อโรงพยาบาลเรื่องนัดฟอกไตล่วงหน้า เผื่อเดินทางไม่ได้")
    if needs.get("oxygen") or needs.get("power_dependent"):
        out.append("เตรียมไฟสำรองหรือแผนย้ายไปที่มีไฟฟ้า สำหรับอุปกรณ์ทางการแพทย์")
    if assets.get("car") and urgent:
        out.append("ย้ายรถไปจอดที่สูงตั้งแต่ตอนนี้")
    if hh.get("pets"):
        out.append("เตรียมกรงหรือสายจูงสัตว์เลี้ยง และดูว่าศูนย์พักพิงรับสัตว์เลี้ยงไหม")
    if house.get("stories") == 1 and urgent:
        out.append("บ้านชั้นเดียว: เตรียมที่พักสำรอง เช่น บ้านญาติที่อยู่สูงกว่า")
    return out


LEAD_SYSTEM = """คุณคือ "น้ำมา" ผู้ช่วยเรื่องน้ำท่วม พูดภาษาไทย สุภาพ สงบ ใช้ "ค่ะ" ไม่ใช้ "ครับ"
ตอบคำถามของผู้ใช้ตรงๆ ใน 1-2 ประโยคสั้นๆ โดยใช้คำตัดสินที่ให้มาเป็นหลัก ห้ามเปลี่ยนระดับความเสี่ยง
ขึ้นต้นด้วยคำตอบเลย แล้วตามด้วยเหตุผลสั้นๆ จากข้อมูลที่ให้ ไม่ต้องทักทาย ไม่ใช้ markdown
ห้ามใส่ตัวเลขใดๆ ห้ามบอกว่าปลอดภัย หรือไม่ท่วมแน่นอน"""

_LEAD_BANNED = re.compile(r"ปลอดภัย|ไม่ท่วมแน่|ไม่มีทางท่วม|ไม่ต้อง(?:ห่วง|กังวล)")
_LEAD_CALM = re.compile(r"ไม่น่าห่วง|ไม่น่าท่วม|ไม่ท่วม")


def rule_lead(tier: str, rising: bool, extent: dict | None, approx: bool) -> str:
    """The direct answer above the facts, from the worst tier shown. Leans, never promises: no "safe", no "won't flood"."""
    if tier == "danger":
        return "ความเสี่ยงสูงค่ะ น้ำล้นตลิ่งที่สถานีวัดน้ำใกล้ๆ แล้ว ถ้าบ้านอยู่ที่ลุ่มหรือริมน้ำ ควรย้ายไปที่สูงตอนนี้"
    if tier == "warning":
        return "มีความเสี่ยงค่ะ น้ำที่สถานีวัดน้ำใกล้ๆ ใกล้ถึงตลิ่ง ควรเตรียมพร้อมอพยพ"
    if tier == "watch":
        return "ควรเฝ้าระวังค่ะ น้ำที่สถานีวัดน้ำใกล้ๆ ยังไม่ถึงตลิ่งแต่ใกล้ขึ้นมาแล้ว ควรยกของขึ้นที่สูงไว้ก่อน"
    if extent is None:
        sat = ""
    elif extent["nearest_km"] is None:
        sat = " และภาพดาวเทียมไม่พบน้ำท่วมในบริเวณนี้"
    elif extent["at_point"] and not approx:
        sat = " แต่ภาพดาวเทียมพบน้ำท่วมที่จุดนี้ ควรติดตามใกล้ชิด"
    else:
        sat = " แต่ภาพดาวเทียมพบน้ำท่วมในบริเวณใกล้เคียง ควรติดตามใกล้ชิด"
    tail = " แต่น้ำกำลังขึ้น ควรติดตามต่อ" if rising and not sat.startswith(" แต่") else ""
    return f"ตอนนี้ยังไม่น่าห่วงค่ะ น้ำที่สถานีวัดน้ำใกล้ๆ ยังต่ำกว่าตลิ่ง{sat}{tail}"


def lead_answer(question: str, tier: str, rising: bool, extent: dict | None, approx: bool) -> str:
    """rule_lead, reworded by the LLM to answer the question as asked. The rewrite is rejected if it has a number,
    says "safe" or "won't flood", or sounds calm when the tier is watch or higher."""
    rule = rule_lead(tier, rising, extent, approx)
    if not question.strip() or not llm.available():
        return rule
    out = llm.chat([{"role": "system", "content": LEAD_SYSTEM},
                    {"role": "user", "content": f"คำถาม: {question}\nคำตัดสิน: {rule}\n\nเขียนคำตอบ"}],
                   max_tokens=150)
    if not out:
        return rule
    out = _MARKDOWN.sub("", " ".join(out.split())).replace("ครับ", "ค่ะ")
    if (llm.NUM.search(out) or _LEAD_BANNED.search(out) or len(out) > 200
            or (rank(tier) >= rank("watch") and _LEAD_CALM.search(out))):
        return rule
    return out


def advice_lines(tier: str, memory: dict, facts_text: str) -> list[str]:
    rules = rule_advice(tier, memory)
    if not llm.available():
        return rules
    draft = facts_text + "\n" + "\n".join(rules)
    out = llm.chat([{"role": "system", "content": ADVICE_SYSTEM},
                    {"role": "user", "content": f"ข้อมูล:\n{draft}\n\nเขียนคำแนะนำ"}], max_tokens=300)
    if out and llm.numbers_preserved(draft, out) and "ปลอดภัยแน่นอน" not in out:
        return clean_advice(out) or rules
    return rules


_BULLET = re.compile(r"^\s*(?:[-•*]|\d+[.)])\s*")
_MARKDOWN = re.compile(r"[*#`]+")
_INTRO = re.compile(r"สวัสดี|ดังนี้\s*:?\s*$|^(?:ผม|ดิฉัน)")
_END_PARTICLE = re.compile(r"\s*(?:นะ)?(?:ครับ|ค่ะ|คะ)\s*$")


def clean_advice(out: str) -> list[str]:
    """The LLM's advice as at most 4 bullets: no greeting or lead-in line, no markdown, no ครับ (NamMa says ค่ะ)."""
    items = []
    for ln in out.strip().splitlines():
        ln = _MARKDOWN.sub("", _BULLET.sub("", ln)).strip()
        ln = _END_PARTICLE.sub("", ln).replace("ครับ", "ค่ะ")
        if ln and not _INTRO.search(ln) and not ln.endswith(":"):
            items.append(ln)
    return items[:4]


def _shelter_desc(s: dict, need_patients: bool) -> str:
    """'ชื่อ อ.เขต ห่าง 2.3 กม. (เปิดรับอยู่ …, รับผู้ป่วยติดเตียง)': DDPM status, unknown said as unknown."""
    tags = []
    if s.get("is_open") is True:
        cap, cur = s.get("capacity"), s.get("current_residents")
        tags.append("เปิดรับอยู่" + (f" มีผู้พัก {cur}/{cap} คน" if cap and cur is not None else ""))
    elif s.get("is_open") is False:
        tags.append("ยังไม่เปิด")
    ok = s.get("patients_ok", s.get("accepts_patients") or None)
    if ok:
        tags.append("รับผู้ป่วยติดเตียง")
    elif need_patients:
        tags.append("ไม่ได้ระบุว่ารับผู้ป่วยติดเตียง" if ok is None else "ไม่รับผู้ป่วยติดเตียง")
    if s["distance_km"] > FAR_STATION_KM:
        tags.append("ไกล โทร 1784 ถามศูนย์ที่ใกล้กว่า")
    unit = "เขต" if s.get("province") == "กรุงเทพมหานคร" else "อ."
    where = f" {unit}{s['district']}" if s.get("district") else ""
    return f"{s['name']}{where} ห่าง {s['distance_km']:.1f} กม." + (f" ({', '.join(tags)})" if tags else "")


SHELTER_SOURCE_NOTE = "ข้อมูลศูนย์พักพิงจาก ปภ. สถานะเปลี่ยนได้ โทร 1784 สอบถามก่อนเดินทาง"


def shelter_line(shelters: list[dict], need_patients: bool = False) -> list[str]:
    """The top shelter from tools.find_shelters (best match, not always the nearest) with its DDPM status.
    Unknown is said as unknown: most DDPM shelters don't say whether they take bedridden patients."""
    out = []
    for s in shelters[:1]:
        out.append("🏫 ศูนย์พักพิงที่แนะนำ: " + _shelter_desc(s, need_patients))
        if s.get("source") == "ddpm":
            out.append(SHELTER_SOURCE_NOTE)
    return out


def shelter_answer(place_label: str, approx: bool, shelters: list[dict],
                   need_patients: bool) -> tuple[str, list[WebSwap]]:
    """Answer to "where do I go": the best few shelters near a place, ranked by tools.find_shelters.
    On the web the list is the shelter card, so the text keeps only a heading and the evacuation-order note."""
    if not shelters:
        return (f"ยังไม่พบข้อมูลศูนย์พักพิงใกล้ {place_label} ค่ะ โทร 1784 (ปภ.) สอบถามศูนย์ที่เปิดอยู่ได้ตลอด 24 ชม.\n\n"
                + FOOTER), []
    where = f"ใกล้ {place_label}" + (" (ตำแหน่งประมาณจากชื่อพื้นที่)" if approx else "")
    order = " เรียงให้ศูนย์ที่รับผู้ป่วยติดเตียงขึ้นก่อน" if need_patients else ""
    lines = [f"🏫 ศูนย์พักพิง{where}{order}:"]
    lines += [f"{i}. {_shelter_desc(s, need_patients)}" for i, s in enumerate(shelters, 1)]
    lines.append("")
    lines.append(SHELTER_SOURCE_NOTE if any(s.get("source") == "ddpm" for s in shelters)
                 else "โทร 1784 สอบถามก่อนเดินทางว่าศูนย์ยังรับคนได้")
    lines.append(EVAC_ORDER_NOTE)
    block = "\n".join(lines)
    return block + "\n\n" + FOOTER, [(block, f"ศูนย์พักพิง{where}{order}\n{EVAC_ORDER_NOTE}")]


def risk_answer(place_label: str, approx: bool, stations: list[dict], shelters: list[dict], memory: dict,
                extent: dict | None = None, recurrence: dict | None = None,
                extra: list[str] | None = None, question: str = "") -> tuple[str, list[WebSwap]]:
    """The reply opens with a direct answer to the question (lead_answer), then the facts it rests on.
    extra: follow-up paragraphs (questions, 'saved' notes) placed before the DDPM footer.
    On the web the station lines, GISTDA lines and shelter line become cards and source lines, so the text keeps a
    one-line verdict, the forecast (no card shows it), the far-station and station-not-house notes, and the advice."""
    tail = "".join("\n\n" + e for e in extra or [])
    if not stations:
        return (f"📍 {place_label}\nตอนนี้ไม่พบสถานีวัดน้ำที่มีข้อมูลล่าสุดในระยะใกล้ จึงประเมินให้ไม่ได้\n"
                "ลองแชร์ตำแหน่งที่แม่นขึ้น หรือติดตามประกาศของทางการ" + tail + "\n\n" + FOOTER), []
    main = stations[0]
    worst = max(stations, key=lambda s: rank(s["risk_tier"]))
    shown = [main] + ([worst] if worst is not main and rank(worst["risk_tier"]) > rank(main["risk_tier"]) else [])
    lines = station_lines(main)
    far = None
    if main["distance_km"] > FAR_STATION_KM:
        far = (f"⚠️ สถานีที่ใกล้ที่สุดอยู่ห่าง {main['distance_km']:.1f} กม. อาจไม่ตรงกับสถานการณ์ในพื้นที่ "
               "ใช้ประกอบกับประกาศของทางการในพื้นที่")
        lines.append(far)
    for s in shown[1:]:
        lines.append("")
        lines += station_lines(s)
    satellite = []                                 # about the place itself, not the river: kept on the web too
    if extent is not None:
        satellite.append(flood_extent_text(extent, approx))
    if recurrence is not None and (rec := recurrence_text(recurrence, approx)):
        satellite.append(rec)
    lines += ["🛰️ " + s for s in satellite]
    lines.append(STATION_NOT_HOUSE)
    place = f"📍 {place_label}" + (" (ตำแหน่งโดยประมาณจากชื่อพื้นที่)" if approx else "")
    station_block = "\n".join(lines)
    facts = place + "\n" + station_block
    # Web: the place line heads a list of one fact per bullet (ReplyText), and the caveats follow as their own paragraph.
    web = [verdict(shown[-1])]                     # the worse station when two are shown
    web += [f"แบบจำลองคาดการณ์ ({s['name_th']}): {f}" for s in shown if (f := _forecast(s))]
    web += satellite
    notes = ([far] if far else []) + [STATION_NOT_HOUSE]
    swaps = [(station_block, "\n".join("• " + w for w in web) + "\n\n" + "\n".join(notes))]
    tier = worst["risk_tier"]
    rising = (main.get("trend_3h_cm_per_hr") or 0) >= 0.5
    with ThreadPoolExecutor(2) as pool:          # two LLM calls: run them side by side
        lead = pool.submit(lead_answer, question, tier, rising, extent, approx)
        adv = advice_lines(tier, memory, facts)
        lead = lead.result()
    body = lead + "\n\n" + facts + "\n\n✅ สิ่งที่ควรทำตอนนี้\n" + "\n".join(f"• {a}" for a in adv)
    sh = shelter_line(shelters, need_patients=bool((memory.get("household") or {}).get("immobile")))
    if sh:
        shelter_block = "\n\n" + "\n".join(sh)
        body += shelter_block
        swaps.append((shelter_block, ""))
    return body + tail + "\n\n" + FOOTER, swaps
