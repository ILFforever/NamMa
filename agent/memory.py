"""Memory extraction (CLAUDE.md §7): newly *stated* durable facts only, never inferred.

extract() uses MaaS when configured and falls back to keyword rules, so the demo works offline.
household and needs are PDPA-sensitive: they wait in pending_facts until the user consents.
"""
import re

from agent import llm

SENSITIVE = {"household", "needs"}
FIELDS = ("household", "house", "assets", "needs", "history")

WHO = (r"(คุณยาย|คุณตา|แม่ยาย|พ่อตา|ลูกชาย|ลูกสาว|น้องชาย|น้องสาว|พี่ชาย|พี่สาว|ยาย|ตา|ปู่|ย่า|พ่อ|แม่|ลูก|สามี|"
       r"ภรรยา|เมีย|ผัว|น้อง|พี่|หลาน|ป้า|ลุง|น้า|อา(?!ยุ|การ|หาร))")
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
          r"ท้องแก่|คนท้อง|ท้อง|ฟอกไต|ฟอกเลือด|ล้างไต|ออกซิเจน|เครื่องช่วยหายใจ|ซีแพพ|cpap|กินยา|ยาประจำ|อินซูลิน|"
          r"หมา|แมว|สุนัข|สัตว์เลี้ยง)")
# Before the word: a negation, another household, a past condition, a shelter that takes such people. After it:
# "หายแล้ว" (better now). A relative who lives somewhere else ("ยายอยู่เชียงใหม่ติดเตียง") is another household.
_NOT_OURS = re.compile(r"(ไม่มี|(?<!เดิน)(?<!ขยับ)(?<!ตัว)ไม่ได้|ไม่ใช่|เลิก|หายจาก|เคย|รับ|ที่พักกับ|ศูนย์|"
                       r"เพื่อนบ้าน|ข้างบ้าน|บ้านข้างๆ|บ้านอื่น|บ้านเขา|บ้านเค้า|บ้านเพื่อน|เพื่อน|" + WHO + r"อยู่(?!บ้าน|ด้วย|กับ|ที่บ้าน)\S{2,14}?)"
                       r"\S{0,8}?" + _TERMS + r"|" + _TERMS + r"\S{0,12}?(หายแล้ว|หายดีแล้ว|ตอนนี้หาย)")
_NEGATED = re.compile(r"(ไม่มี|(?<!เดิน)(?<!ขยับ)(?<!ตัว)ไม่ได้|ไม่ใช่|เลิก|พิมพ์ผิด)\S{0,8}?" + _TERMS)
_HYPOTHETICAL = re.compile(r"ถ้า|หาก|สมมติ")
# Taken back in the same message: "…ใช้เครื่องช่วยหายใจ อ้อ จริงๆ ไม่ใช่นะ ไม่มีใครใช้".
_RETRACTED = re.compile(r"ไม่ใช่นะ|ไม่ใช่ค่ะ|ไม่ใช่ครับ|พิมพ์ผิด|ลืมไป|ไม่มีใคร|ขอแก้|ยกเลิกที่พูด|ไม่จริง")
# "ขอแก้ไข จริงๆ เป็นพี่สาวไม่ใช่แม่": a correction of what was saved.
CORRECTION = re.compile(r"ขอแก้|แก้ไข|พิมพ์ผิด|จำผิด|ไม่ใช่" + WHO)


def extract_rules(text: str) -> dict:
    t = _NOT_OURS.sub("", text.replace(" ", ""))
    hh, needs, house, assets, hist = {}, {}, {}, {}, {}
    who = [m.group(1) for m in re.finditer(WHO + r"\S{0,10}?" + IMMOBILE, t)]
    if not who and (m := re.search(IMMOBILE, t)):          # "ติดเตียงทั้งพ่อทั้งแม่"
        who = re.findall(WHO, t[m.end():m.end() + 24])
    if who:
        hh["immobile"] = sorted(set(who))
    elif re.search(IMMOBILE, t):
        hh["immobile"] = ["สมาชิกในบ้าน"]
    if re.search(r"ผู้สูงอายุ|คนแก่|คนชรา", t):
        hh["elderly"] = True
    young = [int(n) for n in re.findall(r"(?:ลูก|หลาน)\S{0,12}?(\d+)(?:ขวบ|ปี)", t) + re.findall(r"(\d+)ขวบ", t)]
    if (re.search(r"เด็กเล็ก|ลูกเล็ก|ทารก|ลูกอ่อน|ลูกคนเล็ก|(ลูก|หลาน)\S{0,6}?\d+เดือน", t)
            or any(n <= 6 for n in young)):
        hh["children"] = True
    if re.search(r"ตั้งครรภ์|ท้องแก่|คนท้อง|ท้อง\d+เดือน|(ภรรยา|เมีย|แฟน|ลูกสาว|ลูกสะใภ้)\S{0,4}?ท้อง", t):
        hh["pregnant"] = True
    pets = re.findall(r"(?:(?<!ไม่)มี|เลี้ยง)\S{0,6}?(หมา|แมว|สุนัข|สัตว์เลี้ยง)(?!จร)", t)
    if pets and not re.search(r"(ยังไง|อย่างไร|ไหม|มั้ย|ที่ไหน)$", t):
        hh["pets"] = sorted(set(pets))
    if "ฟอกไต" in t or "ล้างไต" in t or "ฟอกเลือด" in t:
        needs["dialysis"] = True
    if "ออกซิเจน" in t:
        needs["oxygen"] = True
    if re.search(r"เครื่องช่วยหายใจ|ใช้ไฟฟ้า|ซีแพพ|cpap|ต้องใช้ไฟ", t, re.I):
        needs["power_dependent"] = True
    if re.search(r"ยาประจำ|ยาโรค|อินซูลิน|ยาความดัน|ยาเบาหวาน|กินยา(ประจำ|ทุกวัน|ตลอด|โรค)", t):
        needs["medication"] = True
    if re.search(r"บ้านชั้นเดียว|บ้าน1ชั้น|ชั้นหนึ่ง|ชั้นเดียว", t):
        house["stories"] = 1
    elif re.search(r"บ้านสองชั้น|บ้าน2ชั้น", t):
        house["stories"] = 2
    if re.search(r"ใต้ถุนสูง|ยกพื้น|ยกสูง", t):
        house["raised"] = True
    near = re.search(r"ติดคลอง|ริมคลอง|ใกล้คลอง|ริมแม่น้ำ|ติดแม่น้ำ|ใกล้แม่น้ำ|ริมน้ำ", t)
    if near and not re.search(r"(ร้าน|โรงเรียน|ที่ทำงาน|ตลาด|วัด|โรงงาน|ออฟฟิศ|บ้าน(คุณ)?" + WHO + r")\S{0,10}$",
                              t[:near.start()]):
        house["near_water"] = True
    if re.search(r"(?<!ไม่)มีรถ(ยนต์|เก๋ง|กระบะ|(?!มอ|เข็น))|ขับรถ(ยนต์|เก๋ง|กระบะ)", t):
        assets["car"] = True
    if re.search(r"(?<!ไม่)มี(รถ)?(มอเตอร์ไซค์|มอไซ|จักรยานยนต์)|ขับ(มอเตอร์ไซค์|มอไซ)", t):
        assets["motorbike"] = True
    m = (re.search(r"((?:ปี)?\s*(?:25)?54|2011)\S{0,10}(ท่วม|น้ำเข้า)\S{0,15}", t)
         or re.search(r"(ท่วม|น้ำเข้า)\S{0,6}ปี(25)?54|(ท่วม|น้ำเข้า)\S{0,6}ปี2011", t))
    if m:
        hist["past_floods"] = [m.group(0)]
    facts = {k: v for k, v in zip(FIELDS, (hh, house, assets, needs, hist)) if v}
    return _drop_hypothetical(text, facts)


def negates_sensitive(text: str) -> bool:
    """"ยายไม่ได้ติดเตียง", "เมื่อกี้พิมพ์ผิด": a correction, so facts waiting for consent are dropped."""
    return bool(_NEGATED.search(text.replace(" ", "")))


def _drop_hypothetical(text: str, facts: dict) -> dict:
    """"ถ้าน้ำท่วม แม่จะติดเตียง" states nothing about the household: keep no sensitive fact from an if-sentence."""
    if _HYPOTHETICAL.search(text) or _RETRACTED.search(text.replace(" ", "")):
        return {k: v for k, v in facts.items() if k not in SENSITIVE}
    return facts


def clean(text: str, out: dict) -> dict:
    """LLM output → the known fields only, nothing sensitive from an if-sentence."""
    return _drop_hypothetical(text, {k: v for k, v in out.items() if k in FIELDS and isinstance(v, dict) and v})


def extract(text: str) -> dict:
    if llm.available():
        out = llm.chat_json(SYSTEM, text)
        if isinstance(out, dict):
            return clean(text, out)
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
    """'บ้านแม่อยู่บางระกำ' → 'บ้านแม่'. First-person homes collapse to 'บ้าน'. Labels other than บ้าน need
    "อยู่"/"แถว" ("ร้านน้ำแข็งที่หัวหิน" is not a saved place); "ชาวบ้าน", "หมู่บ้าน" are not homes."""
    t = text.replace(" ", "")
    m = (re.search(r"(?<!ชาว)(?<!หมู่)(?<!เพื่อน)(?<!ข้าง)(?<!กลับ)(บ้าน\S{0,12}?)(?:อยู่|ที่|แถว|คือ)", t)
         or re.search(r"((?:ร้าน|โรงเรียน|คอนโด|หอพัก|ที่ทำงาน|ออฟฟิศ|โรงงาน|สวน|ไร่)\S{0,12}?)(?:อยู่|แถว|คือ)", t))
    if not m:
        return None
    if re.match(r"(ใกล้|ติด|ริม|ชั้น|คนเดียว|ด้วย|กับ|สูง|ต่ำ|ในซอย)", t[m.end():]):
        return None                                       # "บ้านอยู่ใกล้แม่น้ำ" describes the home, not where it is
    label = re.sub(r"(ไว้|ให้|ด้วย|ได้ไหม|ได้มั้ย|หน่อย|นะ|ที|จ้า|ค่ะ|ครับ|ย้ายไป|ย้ายมา|ตอนนี้|จริงๆ|"
                   r"ของ(ผม|ฉัน|เรา|หนู|ดิฉัน))+$", "", m.group(1))
    if re.fullmatch(r"บ้าน(ผม|ฉัน|หนู|เรา|ดิฉัน|กู|เค้า|ตัวเอง|ข้า|เอง)?", label) or re.match(r"บ้าน(ผม|ฉัน|หนู|เรา)", label):
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
