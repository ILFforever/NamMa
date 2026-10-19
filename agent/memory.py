"""Memory extraction (CLAUDE.md §7): newly *stated* durable facts only, never inferred.

extract() uses MaaS when configured and falls back to keyword rules, so the demo works offline.
household and needs are PDPA-sensitive: they wait in pending_facts until the user consents.
"""
import re

from agent import llm

SENSITIVE = {"household", "needs"}
FIELDS = ("household", "house", "assets", "needs", "history")

WHO = r"(คุณยาย|คุณตา|ยาย|ตา|ปู่|ย่า|พ่อ|แม่|ลูก|สามี|ภรรยา|น้อง|พี่|หลาน|ป้า|ลุง|น้า|อา)"
IMMOBILE = r"(ติดเตียง|เดินไม่ได้|นั่งรถเข็น|ป่วยติดเตียง|อัมพาต)"

SYSTEM = """You extract durable facts a Thai user has explicitly stated about their household for a flood assistant.
Return JSON only, with any of these keys (omit empty ones), or {} if nothing new was stated:
{"household": {"immobile": [who], "elderly": true, "children": true, "pregnant": true, "pets": [kind]},
 "needs": {"dialysis": true, "oxygen": true, "medication": true, "power_dependent": true},
 "house": {"stories": int, "raised": true, "near_water": true},
 "assets": {"car": true, "motorbike": true, "ground_floor_shop": true},
 "history": {"past_floods": [short Thai text]}}
Never infer: "my mother is 80" is NOT a health condition. Only what is literally stated.
Not facts: negations ("ไม่มีคนติดเตียง"), other households ("เพื่อนบ้านมีคนติดเตียง"), hypotheticals ("ถ้า…"),
and questions ("ที่ไหนรับคนติดเตียง")."""

# What a sensitive or household word is NOT a fact about the person's own home after: a negation, another household,
# or a shelter that takes such people ("รับคนติดเตียง"). "เดินไม่ได้" itself is a fact, so "ไม่ได้" after เดิน/ขยับ is not.
_TERMS = (r"(ติดเตียง|เดินไม่ได้|นั่งรถเข็น|อัมพาต|ผู้สูงอายุ|คนแก่|คนชรา|เด็กเล็ก|ลูกเล็ก|ทารก|ลูกอ่อน|ตั้งครรภ์|"
          r"ท้องแก่|คนท้อง|ฟอกไต|ล้างไต|ออกซิเจน|เครื่องช่วยหายใจ|กินยา|ยาประจำ|หมา|แมว|สุนัข|สัตว์เลี้ยง)")
_NOT_OURS = re.compile(r"(ไม่มี|(?<!เดิน)(?<!ขยับ)(?<!ตัว)ไม่ได้|ไม่ใช่|เลิก|หายจาก|รับ|เพื่อนบ้าน|ข้างบ้าน|บ้านข้างๆ|"
                       r"บ้านอื่น|เพื่อน)\S{0,8}?" + _TERMS)
_HYPOTHETICAL = re.compile(r"ถ้า|หาก|สมมติ")


def extract_rules(text: str) -> dict:
    t = _NOT_OURS.sub("", text.replace(" ", ""))
    hh, needs, house, assets, hist = {}, {}, {}, {}, {}
    who = [m.group(1) for m in re.finditer(WHO + r"\S{0,6}?" + IMMOBILE, t)]
    if who:
        hh["immobile"] = sorted(set(who))
    elif re.search(IMMOBILE, t):
        hh["immobile"] = ["สมาชิกในบ้าน"]
    if re.search(r"ผู้สูงอายุ|คนแก่|คนชรา", t):
        hh["elderly"] = True
    if re.search(r"เด็กเล็ก|ลูกเล็ก|ทารก|ลูกอ่อน", t) or any(int(n) <= 6 for n in re.findall(r"(\d+)ขวบ", t)):
        hh["children"] = True
    if re.search(r"ตั้งครรภ์|ท้องแก่|คนท้อง", t):
        hh["pregnant"] = True
    pets = re.findall(r"(หมา|แมว|สุนัข|สัตว์เลี้ยง)", t)
    if pets and re.search(r"มี|เลี้ยง", t):
        hh["pets"] = sorted(set(pets))
    if "ฟอกไต" in t or "ล้างไต" in t:
        needs["dialysis"] = True
    if "ออกซิเจน" in t:
        needs["oxygen"] = True
    if "เครื่องช่วยหายใจ" in t or "ใช้ไฟฟ้า" in t:
        needs["power_dependent"] = True
    if re.search(r"กินยา|ยาประจำ|ยาโรค", t):
        needs["medication"] = True
    if re.search(r"บ้านชั้นเดียว|บ้าน1ชั้น", t):
        house["stories"] = 1
    elif re.search(r"บ้านสองชั้น|บ้าน2ชั้น", t):
        house["stories"] = 2
    if "ใต้ถุนสูง" in t:
        house["raised"] = True
    if re.search(r"ติดคลอง|ริมคลอง|ใกล้คลอง|ริมแม่น้ำ|ติดแม่น้ำ|ใกล้แม่น้ำ|ริมน้ำ", t):
        house["near_water"] = True
    if re.search(r"มีรถยนต์|มีรถเก๋ง|มีรถกระบะ|มีรถ(?!มอ)", t):
        assets["car"] = True
    if re.search(r"มอเตอร์ไซค์|มอไซ|จักรยานยนต์", t):
        assets["motorbike"] = True
    m = re.search(r"((?:ปี)?\s*(?:25)?54|2011)\S{0,10}(ท่วม|น้ำเข้า)\S{0,15}", t)
    if m:
        hist["past_floods"] = [m.group(0)]
    facts = {k: v for k, v in zip(FIELDS, (hh, house, assets, needs, hist)) if v}
    return _drop_hypothetical(text, facts)


def _drop_hypothetical(text: str, facts: dict) -> dict:
    """"ถ้าน้ำท่วม แม่จะติดเตียง" states nothing about the household: keep no sensitive fact from an if-sentence."""
    if _HYPOTHETICAL.search(text):
        return {k: v for k, v in facts.items() if k not in SENSITIVE}
    return facts


def extract(text: str) -> dict:
    if llm.available():
        out = llm.chat_json(SYSTEM, text)
        if isinstance(out, dict):
            return _drop_hypothetical(text, {k: v for k, v in out.items() if k in FIELDS and isinstance(v, dict) and v})
    return extract_rules(text)


def split_sensitive(facts: dict) -> tuple[dict, dict]:
    return ({k: v for k, v in facts.items() if k not in SENSITIVE},
            {k: v for k, v in facts.items() if k in SENSITIVE})


def merged_view(memory: dict, pending: dict) -> dict:
    """Memory plus not-yet-consented facts, for use in *this* conversation only."""
    out = {k: dict(memory.get(k) or {}) for k in FIELDS}
    for k, v in (pending or {}).items():
        out.setdefault(k, {}).update(v)
    return out


def location_label(text: str) -> str | None:
    """'บ้านแม่อยู่บางระกำ' → 'บ้านแม่'. First-person homes collapse to 'บ้าน'."""
    t = text.replace(" ", "")
    m = re.search(r"(บ้าน\S{0,12}?)(?:อยู่|ที่|แถว)", t)
    if not m:
        return None
    label = m.group(1)
    if re.fullmatch(r"บ้าน(ผม|ฉัน|หนู|เรา|ดิฉัน|กู|เค้า|ตัวเอง)?", label):
        return "บ้าน"
    return label


def describe(memory: dict) -> list[str]:
    """Thai lines for the profile view."""
    hh, needs, house, assets, hist = (memory.get(k) or {} for k in ("household", "needs", "house", "assets", "history"))
    lines = []
    if hh.get("immobile"):
        lines.append("ผู้ที่เดินไม่ได้/ติดเตียง: " + ", ".join(hh["immobile"]))
    for k, th in (("elderly", "มีผู้สูงอายุ"), ("children", "มีเด็กเล็ก"), ("pregnant", "มีคนตั้งครรภ์")):
        if hh.get(k):
            lines.append(th)
    if hh.get("pets"):
        lines.append("สัตว์เลี้ยง: " + ", ".join(hh["pets"]))
    for k, th in (("dialysis", "ต้องฟอกไต"), ("oxygen", "ใช้ออกซิเจน"), ("power_dependent", "ใช้อุปกรณ์ที่ต้องใช้ไฟฟ้า"),
                  ("medication", "มียาประจำ")):
        if needs.get(k):
            lines.append(th)
    if house.get("stories"):
        lines.append(f"บ้าน {house['stories']} ชั้น")
    if house.get("raised"):
        lines.append("บ้านใต้ถุนสูง")
    if house.get("near_water"):
        lines.append("บ้านติดคลอง/ริมน้ำ")
    if assets.get("car"):
        lines.append("มีรถยนต์")
    if assets.get("motorbike"):
        lines.append("มีรถจักรยานยนต์")
    for h in hist.get("past_floods") or []:
        lines.append(f"เคยท่วม: {h}")
    return lines
