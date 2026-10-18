"""Thai answer text. Facts lines are templated from tool output (rule 1: numbers only from tools);
the LLM, when configured, may only rewrite the advice paragraph, and is rejected if it adds numbers.
"""
from datetime import datetime

from agent import llm
from agent.risk import TIER_TH, rank

FOOTER = ("ℹ️ น้ำมาเป็นผู้ช่วยไม่เป็นทางการ โปรดติดตามประกาศและทำตามคำสั่งอพยพของทางการเสมอ\n"
          "☎️ สายด่วน ปภ. 1784 • LINE @1784DDPM")
TIER_ICON = {"normal": "🟢", "watch": "🟡", "warning": "🟠", "danger": "🔴"}

ADVICE_SYSTEM = """คุณคือ "น้ำมา" ผู้ช่วยเรื่องน้ำท่วม พูดภาษาไทย สุภาพ สงบ ตรงประเด็น
เขียนคำแนะนำ "สิ่งที่ควรทำตอนนี้" 2-4 ข้อสั้นๆ สำหรับครอบครัวนี้ จากข้อมูลที่ให้เท่านั้น
ห้ามใส่ตัวเลขใดๆ ที่ไม่มีในข้อมูล ห้ามบอกว่าปลอดภัยแน่นอน ห้ามบอกให้เพิกเฉยต่อคำสั่งอพยพของทางการ"""


def _ts(s: str | None) -> str:
    if not s:
        return "-"
    d = datetime.fromisoformat(s)
    return f"{d:%H:%M} น. {d.day}/{d.month}"


def station_lines(s: dict) -> list[str]:
    lines = [f"📡 สถานีวัดน้ำ {s['code']} {s['name_th']} ({s['river'] or 'ไม่ระบุลำน้ำ'}) ห่าง {s['distance_km']:.1f} กม."]
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
    m = s.get("model") or {}
    parts = []
    for h in ("12", "24"):
        p = m.get(h)
        if p and p.get("diff_wl_bank") is not None:
            d = p["diff_wl_bank"]
            parts.append(f"+{h} ชม. {'สูงกว่าตลิ่ง' if d >= 0 else 'ต่ำกว่าตลิ่ง'} {abs(d):.2f} ม. (±{p['mae_cm']:.0f} ซม.)")
        elif p:
            parts.append(f"+{h} ชม. ระดับ {p['level']:.2f} ม.รทก. (±{p['mae_cm']:.0f} ซม.)")
    if parts:
        lines.append("• แบบจำลองคาดการณ์: " + " · ".join(parts))
    if s.get("hours_to_bank") is not None:
        lines.append(f"• ถ้าขึ้นต่อในอัตรานี้ จะถึงระดับตลิ่งในราว {s['hours_to_bank']:.0f} ชม. (ประมาณการ)")
    for u in s.get("upstream") or []:
        if u.get("risk_tier") == "danger":
            eta = f" น้ำใช้เวลาราว {u['approx_travel_hours']:.0f} ชม. มาถึง" if u.get("approx_travel_hours") else ""
            lines.append(f"• ต้นน้ำ {u['oldcode']} {u['name_th']} ล้นตลิ่งแล้ว{eta}")
    lines.append(f"{TIER_ICON[s['risk_tier']]} ระดับความเสี่ยงที่สถานี: {TIER_TH[s['risk_tier']]}")
    lines.append(f"ข้อมูล ณ {_ts(s.get('reading_ts'))} จาก ThaiWater (สสน.) / {s['agency'] or '-'}")
    return lines


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


def advice_lines(tier: str, memory: dict, facts_text: str) -> list[str]:
    rules = rule_advice(tier, memory)
    if not llm.available():
        return rules
    draft = facts_text + "\n" + "\n".join(rules)
    out = llm.chat([{"role": "system", "content": ADVICE_SYSTEM},
                    {"role": "user", "content": f"ข้อมูล:\n{draft}\n\nเขียนคำแนะนำ"}], max_tokens=300)
    if out and llm.numbers_preserved(draft, out) and "ปลอดภัยแน่นอน" not in out:
        return [ln.strip(" -•*") for ln in out.strip().splitlines() if ln.strip()]
    return rules


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


def shelter_answer(place_label: str, approx: bool, shelters: list[dict], need_patients: bool) -> str:
    """Answer to "where do I go": the best few shelters near a place, ranked by tools.find_shelters."""
    if not shelters:
        return (f"ยังไม่พบข้อมูลศูนย์พักพิงใกล้ {place_label} ค่ะ โทร 1784 (ปภ.) สอบถามศูนย์ที่เปิดอยู่ได้ตลอด 24 ชม.\n\n"
                + FOOTER)
    head = f"🏫 ศูนย์พักพิงใกล้ {place_label}" + (" (ตำแหน่งประมาณจากชื่อพื้นที่)" if approx else "")
    if need_patients:
        head += " เรียงให้ศูนย์ที่รับผู้ป่วยติดเตียงขึ้นก่อน"
    lines = [head + ":"]
    lines += [f"{i}. {_shelter_desc(s, need_patients)}" for i, s in enumerate(shelters, 1)]
    lines.append("")
    lines.append(SHELTER_SOURCE_NOTE if any(s.get("source") == "ddpm" for s in shelters)
                 else "โทร 1784 สอบถามก่อนเดินทางว่าศูนย์ยังรับคนได้")
    lines.append("ถ้ามีคำสั่งอพยพจากทางการ ให้ไปตามจุดที่ทางการกำหนด")
    return "\n".join(lines) + "\n\n" + FOOTER


def risk_answer(place_label: str, approx: bool, stations: list[dict], shelters: list[dict], memory: dict,
                extent: dict | None = None, recurrence: dict | None = None, extra: list[str] | None = None) -> str:
    """extra: follow-up paragraphs (questions, 'saved' notes) placed before the DDPM footer."""
    tail = "".join("\n\n" + e for e in extra or [])
    if not stations:
        return (f"📍 {place_label}\nตอนนี้ไม่พบสถานีวัดน้ำที่มีข้อมูลล่าสุดในระยะใกล้ จึงประเมินให้ไม่ได้\n"
                "ลองแชร์ตำแหน่งที่แม่นขึ้น หรือติดตามประกาศของทางการ" + tail + "\n\n" + FOOTER)
    main = stations[0]
    worst = max(stations, key=lambda s: rank(s["risk_tier"]))
    lines = [f"📍 {place_label}" + (" (ตำแหน่งโดยประมาณจากชื่อพื้นที่)" if approx else "")]
    lines += station_lines(main)
    if worst is not main and rank(worst["risk_tier"]) > rank(main["risk_tier"]):
        lines.append("")
        lines += station_lines(worst)
    if extent is not None:
        lines.append(f"🛰️ ภาพดาวเทียม GISTDA 1 วันล่าสุด: {'พบพื้นที่น้ำท่วมที่จุดนี้' if extent['flooded_last_1day'] else 'ไม่พบน้ำท่วมที่จุดนี้'}")
    if recurrence is not None and recurrence.get("records"):
        lines.append(f"🛰️ GISTDA 2011–2023: จุดนี้เคยมีบันทึกน้ำท่วม {recurrence['records']} ครั้ง")
    lines.append("⚠️ ค่านี้วัดที่สถานีริมแม่น้ำ ไม่ใช่ระดับน้ำที่บ้านโดยตรง ใช้เป็นการประเมินเท่านั้น")
    facts = "\n".join(lines)
    tier = worst["risk_tier"]
    adv = advice_lines(tier, memory, facts)
    body = facts + "\n\n✅ สิ่งที่ควรทำตอนนี้\n" + "\n".join(f"• {a}" for a in adv)
    sh = shelter_line(shelters, need_patients=bool((memory.get("household") or {}).get("immobile")))
    if sh:
        body += "\n\n" + "\n".join(sh)
    return body + tail + "\n\n" + FOOTER
