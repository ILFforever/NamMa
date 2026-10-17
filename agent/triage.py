"""Rescue triage (CLAUDE.md §6): spot danger, extract a ticket, pre-fill from memory.

The 1784 contact goes out first, before any extraction, so a slow model never delays it.
"""
import re

from agent import llm
from agent.memory import extract_rules

DANGER = ["น้ำเข้าบ้าน", "ติดอยู่", "ช่วยด้วย", "ช่วยที", "ออกไม่ได้", "ขอความช่วยเหลือ", "จมน้ำ", "น้ำถึงหลังคา",
          "น้ำถึงเอว", "น้ำถึงอก", "หนีไม่ทัน", "อพยพไม่ได้", "ติดน้ำ", "sos", "ขอเรือ"]
CRITICAL = ["ติดอยู่", "ออกไม่ได้", "จมน้ำ", "หลังคา", "ถึงอก", "หนีไม่ทัน", "หายใจ", "หมดสติ"]
# Regexes, not substrings: "ยา" (medicine) must not match inside "ยาย" (grandma).
NEEDS = {"เรือ": "boat", "อาหาร": "food", "น้ำดื่ม": "drinking_water", r"(?<!ย)ยา(?![ยก])": "medicine",
         "ไฟฟ้า|ไม่มีไฟ": "power", "หมอ|พยาบาล": "medical", r"รถ(?!ยนต์)": "transport"}
NEEDS_TH = {"boat": "เรือ", "food": "อาหาร", "drinking_water": "น้ำดื่ม", "medicine": "ยา", "power": "ไฟฟ้า",
            "medical": "แพทย์", "transport": "รถ", "evacuation": "อพยพ"}
THAI_NUM = {"หนึ่ง": 1, "สอง": 2, "สาม": 3, "สี่": 4, "ห้า": 5, "หก": 6, "เจ็ด": 7, "แปด": 8, "เก้า": 9, "สิบ": 10}

SYSTEM = """Extract a flood rescue request from a Thai message. Return JSON only:
{"people_count": int|null, "vulnerable": [Thai short text], "needs": [one of boat, food, drinking_water, medicine,
 power, medical, transport, evacuation], "urgency": "critical"|"high"}
critical = trapped, water at chest/roof, medical emergency, or someone who cannot walk. Only what is stated."""


def is_danger(text: str) -> bool:
    t = text.replace(" ", "").lower()
    return any(k in t for k in DANGER)


def _people(t: str) -> int | None:
    m = re.search(r"(\d+)\s*คน", t)
    if m:
        return int(m.group(1))
    for w, n in THAI_NUM.items():
        if f"{w}คน" in t:
            return n
    return None


def _vulnerable(household: dict) -> list[str]:
    out = [f"{w} (เดินไม่ได้/ติดเตียง)" for w in household.get("immobile") or []]
    for k, th in (("elderly", "ผู้สูงอายุ"), ("children", "เด็กเล็ก"), ("pregnant", "หญิงตั้งครรภ์")):
        if household.get(k):
            out.append(th)
    return out


def extract(text: str, memory: dict) -> dict:
    """memory: merged view (stored + this-conversation facts). Returns ticket fields."""
    t = text.replace(" ", "")
    stated = extract_rules(text)
    hh = {**(memory.get("household") or {}), **(stated.get("household") or {})}
    needs_mem = {**(memory.get("needs") or {}), **(stated.get("needs") or {})}
    out = {"people_count": _people(t), "vulnerable": _vulnerable(hh),
           "needs": sorted({v for k, v in NEEDS.items() if re.search(k, t)}) or ["evacuation"],
           "urgency": "critical" if any(k in t for k in CRITICAL) else "high"}
    if llm.available():
        got = llm.chat_json(SYSTEM, text) or {}
        if isinstance(got.get("people_count"), int):
            out["people_count"] = got["people_count"]
        out["vulnerable"] = sorted(set(out["vulnerable"]) | {v for v in got.get("vulnerable") or [] if isinstance(v, str)})
        out["needs"] = sorted(set(out["needs"]) | {n for n in got.get("needs") or [] if n in NEEDS_TH})
        if got.get("urgency") == "critical":
            out["urgency"] = "critical"
    for k, th in (("dialysis", "ต้องฟอกไต"), ("oxygen", "ใช้ออกซิเจน"), ("power_dependent", "อุปกรณ์ต้องใช้ไฟฟ้า")):
        if needs_mem.get(k):
            out["vulnerable"].append(th)
    if out["vulnerable"]:
        out["urgency"] = "critical"      # memory pre-fill: a known bedridden member raises urgency
    return out
