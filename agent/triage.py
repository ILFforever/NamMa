"""Danger messages (CLAUDE.md §6): spot them, and help the person make the call.

NamMa sends no rescue request on anyone's behalf. A danger message gets DDPM 1784 first, then what to tell
1784 when they call (place, who needs help, how many people), taken from the message and from memory.
"""
import re

from agent.memory import extract_rules

DANGER = ["น้ำเข้าบ้าน", "ติดอยู่", "ช่วยด้วย", "ช่วยที", "ออกไม่ได้", "ขอความช่วยเหลือ", "จมน้ำ", "น้ำถึงหลังคา",
          "น้ำถึงเอว", "น้ำถึงอก", "หนีไม่ทัน", "อพยพไม่ได้", "ติดน้ำ", "sos", "ขอเรือ"]
THAI_NUM = {"หนึ่ง": 1, "สอง": 2, "สาม": 3, "สี่": 4, "ห้า": 5, "หก": 6, "เจ็ด": 7, "แปด": 8, "เก้า": 9, "สิบ": 10}


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
