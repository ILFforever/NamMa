"""The LINE map pin + Flex card for a risk answer. Numbers in the card come from the sources, never invented."""
import json

from agent.chat import Reply
from line import cards

SOURCES = [
    {"type": "station", "station_id": 1, "code": "AIT001", "name_th": "อโศก (คลองแสนแสบ)", "distance_km": 2.7,
     "observed_at": "2026-10-19T17:10:00", "river": "คลองแสนแสบ", "agency": "HII", "level_msl": 1.80,
     "diff_wl_bank": -0.59, "bank_msl": 2.39, "trend_cm_per_hr": 1.8, "hours_to_bank": 34.0, "risk_tier": "normal",
     "upstream_over": []},
    {"type": "shelter", "shelter_id": 7, "name": "โรงเรียนสวัสดีวิทยา", "distance_km": 2.8},
    {"type": "gistda", "kind": "flood_recurrence", "observed_at": None, "summary": "จุดนี้เคยมีบันทึกน้ำท่วม 3 ครั้ง (2011–2023)"},
]
PLACE = {"label": "บ้าน", "lat": 13.76, "lon": 100.58, "approx": False, "saved_label": "บ้าน"}


def reply(**kw):
    return Reply("text", sources=kw.get("sources", SOURCES), place=kw.get("place", PLACE), extra=kw.get("extra", []))


def test_pin_then_card():
    msgs = cards.risk_messages(reply())
    assert [m["type"] for m in msgs] == ["location", "flex"]
    pin, card = msgs
    assert (pin["latitude"], pin["longitude"], pin["title"]) == (13.76, 100.58, "บ้าน")
    assert "AIT001" in pin["address"] and len(pin["address"]) <= 100
    dumped = json.dumps(card, ensure_ascii=False)
    for needle in ("1.80", "0.59", "ต่ำกว่าตลิ่ง", "น้ำขึ้น 1.8", "34", "โรงเรียนสวัสดีวิทยา", "3 ครั้ง",
                   "ThaiWater", "tel:1784", "google.com/maps/search/?api=1&query=13.76,100.58",
                   "บันทึกเป็น \\\"บ้าน\\\" แล้ว", "ไม่ใช่ระดับน้ำที่บ้านโดยตรง", "ไม่ใช่หน่วยงานทางการ"):
        assert needle in dumped, needle
    assert len(card["altText"] if "altText" in card else msgs[1]["altText"]) <= 400


def test_extra_notes_become_a_text_message():
    msgs = cards.risk_messages(reply(extra=["👪 ที่บ้านมีผู้สูงอายุไหม?", "ขออนุญาตจำข้อมูล"]))
    assert msgs[-1]["type"] == "text" and "ผู้สูงอายุ" in msgs[-1]["text"] and "ขออนุญาต" in msgs[-1]["text"]


def test_no_station_or_place_means_plain_text():
    assert cards.risk_messages(reply(sources=[SOURCES[1]])) is None
    assert cards.risk_messages(Reply("x", sources=SOURCES)) is None     # no place (e.g. a rescue or shelter reply)


def test_card_not_saved_has_no_saved_line_and_marks_approx():
    place = {**PLACE, "saved_label": None, "approx": True}
    dumped = json.dumps(cards.risk_messages(reply(place=place))[1], ensure_ascii=False)
    assert "บันทึกเป็น" not in dumped and "ประมาณจากชื่อพื้นที่" in dumped
