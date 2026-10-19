"""Danger messages (CLAUDE.md §6): spot them, and help the person make the call.

NamMa sends no rescue request on anyone's behalf. A danger message gets DDPM 1784 first, then what to tell
1784 when they call (place, who needs help, how many people), taken from the message and from memory.
"""
import re

from agent.memory import extract_rules
from agent.text import clean

DANGER = ["น้ำเข้าบ้าน", "ติดอยู่", "ช่วยด้วย", "ช่วยที", "ช่วยแหน่", "ออกไม่ได้", "ขอความช่วยเหลือ", "จมน้ำ",
          "น้ำถึงหลังคา", "น้ำถึงเอว", "น้ำถึงอก", "หนีไม่ทัน", "อพยพไม่ได้", "ติดน้ำ", "sos", "ขอเรือ", "ต้องการเรือ",
          # electricity, collapse, swept away
          "ไฟดูด", "ไฟช็อต", "ไฟฟ้าช็อต", "ไฟรั่ว", "ไฟฟ้ารั่ว", "ถูกน้ำพัด", "น้ำพัด", "น้ำซัด", "เรือล่ม", "เรือคว่ำ",
          "ดินถล่มทับ", "ดินสไลด์ทับ", "ทับบ้าน", "ใต้ซาก", "ติดใต้", "บ้านพัง", "บ้านจะพัง", "บ้านกำลังจะพัง",
          "จะจม", "กำลังจม", "จมแล้ว", "บ้านจม",
          # people missing or stuck
          "หายไปในน้ำ", "หายในน้ำ", "เด็กหาย", "คนหาย", "ลูกหาย", "แม่หาย", "พ่อหาย", "เพื่อนหาย", "ยายหาย", "ตาหาย",
          "หาไม่เจอ", "บนหลังคา", "ขึ้นหลังคา", "ยืนอยู่ในน้ำ", "ติดในรถ", "ติดอยู่ในรถ", "บนต้นไม้", "วิ่งหนี",
          "บ่ไหว",
          # medical
          "เลือดออก", "เลือดไหล", "งูกัด", "งูเห่า", "โดนงู", "หายใจไม่ออก", "หายใจลำบาก", "หายใจไม่ได้", "แก๊สรั่ว",
          "หมดสติ", "ไม่หายใจ", "ชักเกร็ง", "ท้องแตก", "น้ำเดิน", "จะคลอด", "กำลังคลอด", "เป็นลม", "จะล้ม", "จะตาย",
          # English
          "help", "trapped", "stuck", "drown", "emergency", "rescue", "bleeding", "breathe", "unconscious",
          "collapsed", "swept", "missing", "lostmy", "ontheroof", "roofof", "snake", "cantwalk", "can'twalk"]
# Water inside the house or up a body: danger unless it is plainly hypothetical, past or a question about later.
# No "ไม่" between น้ำ and the verb: "น้ำไม่ท่วมบ้าน" is the opposite.
WATER_IN = re.compile(r"(น้ำ|เฮือน)(?:(?!ไม่)\S){0,10}?(เข้าบ้าน|เข้าห้อง|เข้าเฮือน|ไหลเข้า|ทะลัก|ท่วมบ้าน|ท่วมห้อง|"
                      r"ท่วมชั้น|ท่วมเฮือน|ป่ามา|ป่าหลาก|ป่าไหล|เต็ม|"
                      r"(ถึง|ท่วม|สูง|ขึ้น)\S{0,4}?(คอ|อก|เอว|หัว|เข่า|เตียง|หลังคา|บันได|ชั้นสอง|ชั้น2|ชั้นบน|ชั้นล่าง|"
                      r"\d+เมตร|สองเมตร|สามเมตร))")
RISING_INDOORS = re.compile(r"น้ำ\S{0,6}?(กำลังขึ้น|ขึ้นเร็ว|ขึ้นเรื่อยๆ|ขึ้นเรื่อย|กำลังจะมา|มาเร็ว)")
INDOORS = re.compile(r"บ้าน|ชั้น|ห้อง|หลังคา|เตียง|ตอนนี้|ไปไหน|ไปที่ไหน")
STRANDED = re.compile(r"(ไม่มี|ขาด)(อาหาร|ข้าว|น้ำดื่ม|น้ำกิน|ยา|อินซูลิน)\S{0,20}?(วัน|แล้ว|เวียนหัว|จะล้ม)")
LIVE_WIRE = re.compile(r"สายไฟ\S{0,8}?(ขาด|ตก|จุ่ม|ช็อต)")
COLLAPSE = re.compile(r"(กำแพง|ตึก|บ้าน|สะพาน|หลังคา|อาคาร)\S{0,4}?(ถล่ม|พังลง|ทรุดลง)")
# Flooded now with someone who cannot move: "ป้าเดินไม่ได้ น้ำท่วมแล้ว".
FLOODED_NOW = re.compile(r"(น้ำท่วม|น้ำมา|น้ำขึ้น|water)\S{0,6}?(แล้ว|now|stairs|in)")
CANNOT_MOVE = re.compile(r"เดินไม่ได้|ติดเตียง|ขยับไม่ได้|ขยับตัวไม่ได้|นั่งรถเข็น|อัมพาต|cantwalk|can'twalk")
NOW = re.compile(r"แล้ว|กำลัง|ตอนนี้|ด่วน|!|now")
NOT_NOW = re.compile(r"ถ้า|หาก|สมมติ|เมื่อปี|ปี\d|ปี25|ปีที่แล้ว|เคย|เกือบ|ป้องกัน|เตรียมตัว|วิธี|เกิดจาก|คืออะไร|"
                     r"จะ\S{0,12}?(ไหม|มั้ย|มั๊ย|หรือเปล่า|รึเปล่า|เมื่อไหร่|วันไหน)|น้ำจะ|"
                     r"(ไหม|มั้ย|มั๊ย|หรือเปล่า|รึเปล่า|ป่าว)(คะ|ครับ|ค่ะ|จ๊ะ|นะ)?\??$")
# Stories, statistics, drills, volunteering, sayings: never an emergency unless they also say it is happening now.
NOT_REAL = re.compile(r"นิยาย|ในหนัง|ละคร|เรื่องเล่า|สถิติ|ซ้อม|drill|อาสา|สมัคร|ข่าว|"
                      r"(หิว|เหนื่อย|ร้อน|ขำ|ง่วง)จะตาย(แล้ว)?|เกิดจาก|ป้องกัน|คืออะไร|เคย|ปีที่แล้ว|เมื่อปี|เกือบ")
STRONG = ("ช่วยด้วย", "ช่วยที", "ช่วยแหน่", "sos", "helpme", "trapped", "drowning")
THAI_NUM = {"หนึ่ง": 1, "สอง": 2, "สาม": 3, "สี่": 4, "ห้า": 5, "หก": 6, "เจ็ด": 7, "แปด": 8, "เก้า": 9, "สิบ": 10}


def is_danger(text: str) -> bool:
    """Is the person in danger now? Keywords (water, electricity, collapse, missing people, medical, English), water
    inside the house or up to the body, stranded without food or medicine. "ถ้าน้ำเข้าบ้านต้องทำยังไง" (if…),
    "เมื่อปี 54 น้ำเข้าบ้าน" (then), "น้ำจะเข้าบ้านไหม" (will it), stories and drills are not, unless they also say
    it is happening now ("แล้ว", "กำลัง", "ตอนนี้"). A plain call for help always is."""
    t = clean(text).replace(" ", "").lower()
    if t in ("ช่วย", "ช่วยหน่อย", "help", "ฉุกเฉิน", "sos"):
        return True
    now = bool(NOW.search(NOT_REAL.sub("", t)))         # "ปีที่แล้ว" is not "แล้ว"
    if any(k in t for k in STRONG):
        return not NOT_REAL.search(t) or now
    hit = (any(k in t for k in DANGER) or bool(WATER_IN.search(t)) or bool(STRANDED.search(t))
           or bool(LIVE_WIRE.search(t)) or bool(COLLAPSE.search(t))
           or (bool(RISING_INDOORS.search(t)) and bool(INDOORS.search(t)))
           or ("ช่วยหน่อย" in t and "น้ำ" in t)
           or (bool(FLOODED_NOW.search(t)) and bool(CANNOT_MOVE.search(t))))
    if not hit:
        return False
    if NOT_REAL.search(t) and not now:
        return False
    return now or not NOT_NOW.search(t)


def _people(t: str) -> int | None:
    m = re.search(r"(\d+)\s*คน", t)
    if m:
        return int(m.group(1))
    for w, n in THAI_NUM.items():
        if f"{w}คน" in t:
            return n
    return None


def _vulnerable(household: dict, needs: dict) -> list[str]:
    out = [f"{w} (เดินไม่ได้/ติดเตียง)" for w in household.get("immobile") or []]
    for k, th in (("elderly", "ผู้สูงอายุ"), ("children", "เด็กเล็ก"), ("pregnant", "หญิงตั้งครรภ์")):
        if household.get(k):
            out.append(th)
    for k, th in (("dialysis", "ต้องฟอกไต"), ("oxygen", "ใช้ออกซิเจน"), ("power_dependent", "อุปกรณ์ต้องใช้ไฟฟ้า")):
        if needs.get(k):
            out.append(th)
    return out


def call_details(text: str, memory: dict) -> dict:
    """What the person can tell 1784: {people_count, vulnerable}. memory: merged view (stored + this conversation).
    Only what was stated, in the message or before; nothing is guessed."""
    stated = extract_rules(text)
    hh = {**(memory.get("household") or {}), **(stated.get("household") or {})}
    needs = {**(memory.get("needs") or {}), **(stated.get("needs") or {})}
    return {"people_count": _people(text.replace(" ", "")), "vulnerable": _vulnerable(hh, needs)}
