"""Distance math and Thai place-name lookup (data/gazetteer.csv, built by ingest.gazetteer)."""
import csv
import math
import re
from dataclasses import dataclass
from functools import lru_cache

from common.config import ROOT

GAZETTEER_CSV = ROOT / "data" / "gazetteer.csv"

# Short forms people actually type → official province names in ThaiWater geocode.
PROVINCE_ALIASES = {
    "อยุธยา": "พระนครศรีอยุธยา", "อยุทยา": "พระนครศรีอยุธยา", "กรุงเทพ": "กรุงเทพมหานคร", "กทม": "กรุงเทพมหานคร",
    "โคราช": "นครราชสีมา", "สุพรรณ": "สุพรรณบุรี", "นครสวรรค์": "นครสวรรค์", "พิษณุโลก": "พิษณุโลก",
    "อุบล": "อุบลราชธานี", "นนท์": "นนทบุรี", "ปทุมธานี": "ปทุมธานี",
    "bangkok": "กรุงเทพมหานคร", "bkk": "กรุงเทพมหานคร", "ayutthaya": "พระนครศรีอยุธยา",
    "ayuthaya": "พระนครศรีอยุธยา", "chiangmai": "เชียงใหม่", "nonthaburi": "นนทบุรี",
}
# A name only counts where it can start and end a Thai word: not followed by a vowel or tone mark that belongs to it
# ("แม่อาย|ุ 85"), not after a leading vowel ("เ|ลี้ยง"), not after "ชื่อ" ("ผมชื่อบางปะอิน").
_BAD_NEXT = re.compile("[\u0e30-\u0e3a\u0e45\u0e47-\u0e4e]")
_LEAD_VOWELS = "\u0e40\u0e41\u0e42\u0e43\u0e44"
_NOT_AFTER = ("ชื่อ", "ชื่อว่า", "นามสกุล", "คุณ", "นาย", "นาง", "น้อง", "พี่", "เขื่อน", "อุทยาน", "อ่างเก็บน้ำ", "อินท")
# Names that are also ordinary words ("แถวเลย", "ท่าเรือ" = a pier, "นนท์" in "อินทนนท์"): only after อ./ต./จ.,
# อำเภอ/ตำบล/จังหวัด, or as the first word of the message.
AMBIGUOUS = {"เลย", "ตาก", "แพร่", "สอง", "ลอง", "ลี้", "พาน", "ธาตุ", "ท่าเรือ", "แม่น้ำ"}
ADMIN_CUES = ("จังหวัด", "จ.", "อำเภอ", "อ.", "ตำบล", "ต.", "เขต", "แขวง")
# Romanised places people type: replaced by the Thai name before matching.
ROMAN = {"bangban": "บางบาล", "bangbal": "บางบาล", "bangpain": "บางปะอิน"}
# Common misspellings of demo-corridor names.
TYPOS = {"บางบาน": "บางบาล", "อยุทธยา": "อยุธยา", "อยุธา": "อยุธยา", "บางประอิน": "บางปะอิน"}
# Short names are often everyday words ("เลย", "ตาก", "สอง", "ลอง", "ธาตุ"): they need a place word right before them.
CUES = ("จังหวัด", "จ.", "อำเภอ", "อ.", "ตำบล", "ต.", "เขต", "แขวง", "อยู่", "แถว", "ที่", "ย่าน", "ใกล้",
        "ดอย", "เขา", "ตีน", "เชิง", "วัด", "ตลาด", "ถนน", "ถ.", "หมู่บ้าน")
SHORT = 4
def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


@dataclass
class Place:
    province: str
    amphoe: str
    tambon: str
    lat: float
    lon: float

    @property
    def label(self) -> str:
        parts = []
        if self.tambon:
            parts.append(f"ต.{self.tambon}")
        if self.amphoe:
            parts.append(f"อ.{self.amphoe}")
        parts.append(f"จ.{self.province}")
        return " ".join(parts)


# Subdistrict names that are also everyday words ("ย้ายไปบ้านใหม่", "อยู่ในเมือง"): never offered as a guess.
COMMON_WORDS = {"แม่น้ำ", "เมือง", "ในเมือง", "บ้านใหม่", "บ้านเก่า", "บ้านกลาง", "ตลาด", "เวียง", "ในเวียง", "หัวเวียง",
                "ท่าน้ำ", "ริมน้ำ", "บางน้ำ"}


@lru_cache(maxsize=1)
def _gazetteer() -> list[Place]:
    if not GAZETTEER_CSV.exists():
        return []
    with open(GAZETTEER_CSV, encoding="utf-8") as f:
        return [Place(r["province"], r["amphoe"], r["tambon"], float(r["lat"]), float(r["lon"]))
                for r in csv.DictReader(f)]


def _norm(text: str) -> tuple[str, str]:
    """(the text without spaces, lowercased; the text with single spaces) for matching."""
    spaced = " ".join(text.lower().split())
    for en, th in ROMAN.items():                       # "bang ban" and "bangban" alike
        spaced = spaced.replace(en[:4] + " " + en[4:], th).replace(en, th)
    joined = spaced.replace(" ", "")
    for typo, th in TYPOS.items():
        spaced, joined = spaced.replace(typo, th), joined.replace(typo, th)
    return joined, spaced


def _names(name: str, text: str, spaced: str, need_cue: bool = False, own_province: bool = False) -> bool:
    """Is `name` named in the text as a word of its own? Short names (or any name when need_cue) also need a
    place word before them, or to open the message as a word of their own ("น่าน น้ำจะท่วมไหม")."""
    for m in re.finditer(re.escape(name), text):
        i, j = m.start(), m.end()
        if j < len(text) and _BAD_NEXT.match(text[j]):
            continue
        if i > 0 and text[i - 1] in _LEAD_VOWELS:
            continue
        before = text[:i]
        if before.endswith(_NOT_AFTER):
            continue
        opens = i == 0 and (spaced == name or spaced.startswith(name + " "))
        if name in AMBIGUOUS or name in COMMON_WORDS:
            if opens or before.endswith(ADMIN_CUES) or (own_province and before.endswith(CUES)):
                return True
            continue
        if before.endswith(CUES) or opens:
            return True
        if not need_cue and len(name) > SHORT:
            return True
    return False


def place_text(text: str) -> str:
    """The text as find_place reads it: no spaces, lowercased, romanised names and typos in Thai."""
    return _norm(text)[0]


def find_place(text: str) -> Place | None:
    """Best (most specific, mutually consistent) place named in free Thai text, or None.

    Thai has no spaces, so we substring-match every known name, on word edges (_names). Names shorter than 3 chars
    are skipped, and names up to 4 need a place word before them. A district only counts if its province is named
    or unambiguous.
    """
    text, spaced = _norm(text)
    provinces = {v for a, v in PROVINCE_ALIASES.items() if _names(a, text, spaced)}
    places = _gazetteer()
    provinces |= {p.province for p in places if not p.amphoe and _names(p.province, text, spaced)}

    def hit(name: str, province: str = "") -> bool:
        return len(name) >= 3 and _names(name, text, spaced, own_province=province in provinces)

    best, best_score = None, 0
    for p in places:
        score = 0
        if p.tambon:
            # A bare tambon name is often shared across provinces; trust it only with context.
            if not hit(p.tambon, p.province) or not (hit(p.amphoe, p.province) or p.province in provinces):
                continue
            score = 3 + (1 if hit(p.amphoe, p.province) else 0)
        elif p.amphoe:
            if not hit(p.amphoe, p.province):
                continue
            score = 2
        else:
            score = 1 if p.province in provinces else 0
        if provinces:
            score = score + 2 if p.province in provinces else score - 2
        if score > best_score:
            best, best_score = p, score
    return best


def place_candidates(text: str, k: int = 3, skip: tuple[str, ...] = ()) -> list[Place]:
    """Subdistricts named in the text that find_place does not trust alone (no district or province named),
    longest name first: "ตีนดอยสุเทพ" → ต.สุเทพ อ.เมืองเชียงใหม่. Only for asking the person which one they mean,
    so the name must follow a place word ("อยู่", "แถว", "ดอย"…) or be the whole message. A name shared by more than
    two subdistricts, or an everyday word (COMMON_WORDS), is too vague to guess from. skip: saved labels to ignore."""
    for s in sorted(skip, key=len, reverse=True):
        text = text.replace(s, " ")
    text, spaced = _norm(text)
    hits = [p for p in _gazetteer() if p.tambon and len(p.tambon) >= 4 and p.tambon not in COMMON_WORDS
            and _names(p.tambon, text, spaced, need_cue=True)]
    hits = [p for p in hits if sum(q.tambon == p.tambon for q in hits) <= 2]
    return sorted(hits, key=lambda p: -len(p.tambon))[:k]
