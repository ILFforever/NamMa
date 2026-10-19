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
          "บ่ไหว", "ติดในบ้าน", "ติดอยู่ใน", "รถดับกลางน้ำ", "ช่วยส่งอาหาร", "น้ำเข้าแล้ว", "น้ำเข้ามาแล้ว",
          "รถพยาบาล", "พลัดหลง", "น้ำล้อม", "ถูกน้ำล้อม", "จมหมด", "ชั้นล่างจม", "คนเจ็บ", "ไม่ไหวแล้ว",
          "หายใจไม่สะดวก", "เป็นหอบ", "หอบหนัก", "ตกน้ำ", "ตกลงไปในคลอง", "ตกลงไปในน้ำ", "ตกคลอง", "ลอยคอ",
          "กลิ่นแก๊ส", "ช่วยดว้ย", "ช่วยด้วยย", "sinking", "stillinthewater", "electricshock", "uptomyneck",
          "submerged", "cantgetout", "can'tgetout", "keepsrising", "waterisin", "waterisinthe", "secondfloor",
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
                      r"(ถึง|ท่วม|สูง|ขึ้น)\S{0,4}?(คอ|อก|เอว|หัว|เข่า|เตียง|หลังคา|บันได|ประตู|ชั้นสอง|ชั้น2|ชั้นบน|"
                      r"ชั้นล่าง))")
# A water depth ("น้ำสูง 2 เมตร") is danger only indoors or now; alone it may be a river level or a question.
METRES = re.compile(r"น้ำ\S{0,6}?(สูง|ถึง|ท่วม)\S{0,4}?([\d.]+|สอง|สาม)เมตร")
# "ฉุกเฉิน" said, not "ที่พักฉุกเฉิน", "เบอร์ฉุกเฉิน", "กระเป๋าฉุกเฉิน".
URGENT = re.compile(r"(?<!ที่พัก)(?<!เบอร์)(?<!ทางออก)(?<!ชุด)(?<!กระเป๋า)(?<!ศูนย์)(?<!แผน)ฉุกเฉิน(?!ที่ไหน|อยู่ไหน|คือ)")
# "ช่วยกูด้วย", "ช่วยแม่ด้วย"; asking NamMa to report for them ("ช่วยแจ้ง 1784 ให้หน่อย") gets the call guide too.
HELP_ME = re.compile(r"ช่วย(กู|ผม|หนู|ฉัน|เรา|แม่|พ่อ|ลูก|ยาย|ตา|เขา|พวกเรา)ด้วย|"
                     r"แจ้ง\S{0,6}(1784|ปภ|กู้ภัย|เจ้าหน้าที่)\S{0,8}(ให้|แทน)|ส่งเรื่อง\S{0,8}(ให้|แทน)|call1784forme")
ALERT = re.compile(r"เตือน(ฉัน|หน่อย|ด้วย|ผม|หนู)|แจ้งเตือน|alertme|notifyme")
# Over now ("เมื่อวานน้ำเข้าบ้าน ตอนนี้แห้งแล้ว"): not an emergency.
OVER = re.compile(r"แห้งแล้ว|ลดแล้ว|น้ำลงแล้ว|ปลอดภัยแล้ว")
RISING_INDOORS = re.compile(r"น้ำ\S{0,6}?(กำลังขึ้น|ขึ้นเร็ว|ขึ้นเรื่อยๆ|ขึ้นเรื่อย|กำลังจะมา|มาเร็ว|กำลังถึง|กำลังเข้า)")
INDOORS = re.compile(r"บ้าน|ชั้น|ห้อง|หลังคา|เตียง|ตอนนี้|ไปไหน|ไปที่ไหน|ไม่มีไฟ|ไฟดับ")
STRANDED = re.compile(r"(ไม่มี|ขาด|ไม่ได้กิน)(อาหาร|ข้าว|น้ำดื่ม|น้ำกิน|ยา|อินซูลิน)\S{0,20}?(วัน|แล้ว|เวียนหัว|จะล้ม)")
LIVE_WIRE = re.compile(r"สายไฟ\S{0,8}?(ขาด|ตก|จุ่ม|ช็อต)")
COLLAPSE = re.compile(r"(กำแพง|ตึก|บ้าน|สะพาน|หลังคา|อาคาร)\S{0,4}?(ถล่ม|พังลง|ทรุดลง)")
# Flooded now with someone who cannot move: "ป้าเดินไม่ได้ น้ำท่วมแล้ว".
FLOODED_NOW = re.compile(r"(น้ำท่วม|น้ำมา|น้ำขึ้น|น้ำเพิ่ม|water)\S{0,6}?(แล้ว|now|stairs|in|เรื่อย)")
CANNOT_MOVE = re.compile(r"ลุกไม่ได้|เดินไม่ได้|ติดเตียง|ขยับไม่ได้|ขยับตัวไม่ได้|นั่งรถเข็น|อัมพาต|cantwalk|can'twalk")
NOW = re.compile(r"แล้ว|กำลัง|ตอนนี้|ด่วน|!|now")
NOT_NOW = re.compile(r"กลัว|คืนนี้|พรุ่งนี้|ถ้า|หาก|สมมติ|เมื่อปี|ปี\d|ปี25|ปีที่แล้ว|เคย|เกือบ|ป้องกัน|เตรียมตัว|วิธี|เกิดจาก|คืออะไร|"
                     r"จะ\S{0,12}?(ไหม|มั้ย|มั๊ย|หรือเปล่า|รึเปล่า|เมื่อไหร่|วันไหน)|น้ำจะ|"
                     r"(ไหม|มั้ย|มั๊ย|หรือเปล่า|รึเปล่า|ป่าว)(คะ|ครับ|ค่ะ|จ๊ะ|นะ)?\??$")
# Stories, statistics, drills, volunteering, sayings: never an emergency unless they also say it is happening now.
NOT_REAL = re.compile(r"ท่อตัน|ท่อแตก|ท่วมห้องน้ำ|ห้องน้ำท่วม|ค่าน้ำ|ฝัน|เบอร์ไหน|โทรเบอร์|เบอร์อะไร|กันน้ำ|แผน|inwork|lol|ในงาน|งานท่วม|(ปวดหัว|ปวดท้อง)\S{0,4}?จะตาย(อยู่)?(แล้ว)?|นิยาย|ในหนัง|ละคร|เรื่องเล่า|เรื่องสั้น|เขียนเรื่อง|รายงาน|วิจัย|การบ้าน|สถิติ|ซ้อม|drill|"
                      r"อาสา|สมัคร|ข่าว|whatif|"
                      r"(หิว|เหนื่อย|ร้อน|ขำ|ง่วง|เบื่อ|เครียด)\S{0,4}?จะตาย(อยู่)?(แล้ว)?|เกิดจาก|ป้องกัน|คืออะไร|เคย|"
                      r"ปีที่แล้ว|เมื่อปี|เกือบ")
POWER_NEED = re.compile(r"เครื่องช่วยหายใจ|ออกซิเจน|ซีแพพ|cpap|ฟอกไต")
ACTIVITY = re.compile(r"กำลัง(ทำ|เขียน|อ่าน|หา|เรียน|ดู)")
STRONG = ("h3lp", "ช่วยด้วย", "ช่วยที", "ช่วยแหน่", "sos", "helpme", "trapped", "drowning")
THAI_NUM = {"หนึ่ง": 1, "สอง": 2, "สาม": 3, "สี่": 4, "ห้า": 5, "หก": 6, "เจ็ด": 7, "แปด": 8, "เก้า": 9, "สิบ": 10}


def is_danger(text: str) -> bool:
    """Is the person in danger now? Keywords (water, electricity, collapse, missing people, medical, English), water
    inside the house or up to the body, stranded without food or medicine. "ถ้าน้ำเข้าบ้านต้องทำยังไง" (if…),
    "เมื่อปี 54 น้ำเข้าบ้าน" (then), "น้ำจะเข้าบ้านไหม" (will it), stories and drills are not, unless they also say
    it is happening now ("แล้ว", "กำลัง", "ตอนนี้"). A plain call for help always is."""
    words = clean(text).lower().split()
    t = "".join(words)
    letters = re.sub(r"[^\w\u0e00-\u0e7f]", "", t)          # "S.O.S.", "s-o-s"
    if t in ("ช่วย", "ช่วยหน่อย", "help", "ฉุกเฉิน", "sos") or letters in ("sos", "help", "helpme"):
        return True
    if words.count("ช่วย") >= 2 or (words and words[0] == "ช่วย" and len(words) <= 3):
        return True                                       # "ช่วย ช่วย ช่วย", "ช่วย ก"
    if POWER_NEED.search(t) and re.search(r"ไฟดับ|ไฟตก|ไม่มีไฟ", t):
        return True                                       # a breathing machine and no power: plan now, not later
    if ALERT.search(t) or (OVER.search(t) and not any(k in t for k in STRONG)):
        return False
    now = bool(NOW.search(NOT_REAL.sub("", ACTIVITY.sub("", t))))   # "ปีที่แล้ว", "กำลังทำรายงาน" are not now
    if any(k in t for k in STRONG) or HELP_ME.search(t) or URGENT.search(t):
        return not NOT_REAL.search(t) or now
    hit = (any(k in t for k in DANGER) or bool(WATER_IN.search(t)) or bool(STRANDED.search(t))
           or bool(LIVE_WIRE.search(t)) or bool(COLLAPSE.search(t))
           or (bool(RISING_INDOORS.search(t)) and bool(INDOORS.search(t)))
           or ("ช่วยหน่อย" in t and "น้ำ" in t)
           or (bool(FLOODED_NOW.search(t)) and bool(CANNOT_MOVE.search(t)))
           or (bool(METRES.search(t)) and bool(INDOORS.search(t) or NOW.search(t))))
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
