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


def test_extra_notes_become_a_card_after_the_risk_card():
    msgs = cards.risk_messages(reply(extra=["👪 ที่บ้านมีผู้สูงอายุไหม?", "ขออนุญาตจำข้อมูล"]))
    dumped = json.dumps(msgs[-1], ensure_ascii=False)
    assert msgs[-1]["type"] == "flex" and "ผู้สูงอายุ" in dumped and "ขออนุญาต" in dumped


def test_no_station_or_place_means_plain_text():
    assert cards.risk_messages(reply(sources=[SOURCES[1]])) is None
    assert cards.risk_messages(Reply("x", sources=SOURCES)) is None     # no place (e.g. a rescue or shelter reply)


def test_card_not_saved_has_no_saved_line_and_marks_approx():
    place = {**PLACE, "saved_label": None, "approx": True}
    dumped = json.dumps(cards.risk_messages(reply(place=place))[1], ensure_ascii=False)
    assert "บันทึกเป็น" not in dumped and "ประมาณจากชื่อพื้นที่" in dumped


# The LightGBM horizons as model.forecast.score writes them and tools.get_nearest_stations returns them.
MODEL = {"12": {"level": 4.95, "mae_cm": 8.4, "diff_wl_bank": -0.12},
         "24": {"level": 5.10, "mae_cm": 12.6, "diff_wl_bank": 0.03}}
FORECAST_ROW = "แบบจำลองคาดการณ์"


def _rows(card) -> list[str]:
    """The labels of the station's water rows, top to bottom."""
    water = card["body"]["contents"][3]["contents"]
    return [r["contents"][0]["text"] for r in water]


def test_modelled_station_card_shows_forecast_after_trend():
    msgs = cards.risk_messages(reply(sources=[{**SOURCES[0], "model": MODEL}] + SOURCES[1:]))
    card = msgs[1]["contents"]
    assert _rows(card) == ["ระดับน้ำ", "3 ชม.ล่าสุด", FORECAST_ROW, "ถึงตลิ่ง"]
    value = card["body"]["contents"][3]["contents"][2]["contents"][1]["text"]
    assert value == "+12 ชม. ต่ำกว่าตลิ่ง 0.12 ม. (±8 ซม.)\n+24 ชม. สูงกว่าตลิ่ง 0.03 ม. (±13 ซม.)"
    assert len(json.dumps(card, ensure_ascii=False).encode()) < 30_000      # LINE's limit for one bubble


def test_station_without_model_card_has_no_forecast():
    for st in (SOURCES[0], {**SOURCES[0], "model": None}):
        card = cards.risk_messages(reply(sources=[st] + SOURCES[1:]))[1]["contents"]
        assert FORECAST_ROW not in json.dumps(card, ensure_ascii=False)
        assert _rows(card) == ["ระดับน้ำ", "3 ชม.ล่าสุด", "ถึงตลิ่ง"]


def test_forecast_reaches_card_through_chat_sources():
    """get_nearest_stations → chat._sources → card: the station Source carries the model, as the reply text does."""
    from agent import chat
    station = {"station_id": 2609, "code": "C.35", "name_th": "บ้านป้อม", "river": "แม่น้ำเจ้าพระยา", "agency": "RID",
               "distance_km": 3.2, "reading_ts": "2026-10-14T15:00:00+07:00", "level_msl": 4.9, "diff_wl_bank": -0.2,
               "trend_3h_cm_per_hr": 1.2, "hours_to_bank": 17.0, "risk_tier": "watch", "upstream": [], "model": MODEL}
    for st, shown in ((station, True), ({**station, "model": None}, False)):
        sources = chat._sources([st], [], None, None, approx=False)
        assert sources[0]["model"] == st["model"]
        dumped = json.dumps(cards.risk_messages(reply(sources=sources))[1], ensure_ascii=False)
        assert ("+12 ชม. ต่ำกว่าตลิ่ง 0.12 ม. (±8 ซม.)" in dumped) is shown


# ---- Replies without a station: the same text as a Flex bubble. ----
from agent import answer, chat  # noqa: E402


def _texts(node) -> list[str]:
    if isinstance(node, dict):
        return ([node["text"]] if node.get("type") == "text" else []) + [t for v in node.values() for t in _texts(v)]
    if isinstance(node, list):
        return [t for v in node for t in _texts(v)]
    return []


def _uris(node) -> list[str]:
    dumped = json.dumps(node, ensure_ascii=False)
    return [u.split('"')[0] for u in dumped.split('"uri": "')[1:]]


def test_text_reply_keeps_every_line_and_turns_the_footer_into_buttons():
    text = "🏫 ศูนย์พักพิงใกล้ บางบาล:\n1. โรงเรียน ห่าง 1.2 กม.\n2. วัด ห่าง 3.4 กม.\n\n⚠️ โทร 1784 ก่อน\n\n" + answer.FOOTER
    (msg,) = cards.text_messages(Reply(text))
    bubble = msg["contents"]
    assert msg["type"] == "flex" and msg["altText"] == "🏫 ศูนย์พักพิงใกล้ บางบาล:"
    assert _texts(bubble["header"]) == ["🏫 ศูนย์พักพิงใกล้ บางบาล"]
    body = " ".join(_texts(bubble["body"]))
    for needle in ("โรงเรียน ห่าง 1.2 กม.", "วัด ห่าง 3.4 กม.", "⚠️ โทร 1784 ก่อน"):
        assert needle in body
    assert "tel:1784" in _uris(bubble["footer"]) and cards.LINE_URL in _uris(bubble["footer"])
    assert answer.FOOTER not in json.dumps(bubble, ensure_ascii=False)


def test_untitled_reply_gets_the_brand_band_and_no_footer_without_one():
    (msg,) = cards.text_messages(Reply("รับทราบค่ะ บันทึกแล้ว"))
    assert _texts(msg["contents"]["header"]) == [cards.BRAND] and "footer" not in msg["contents"]


def test_emergency_puts_the_calls_first_in_red():
    text = chat.RESCUE_HOTLINE + "\n\nน้ำมาไม่ได้ส่งเรื่องให้หน่วยกู้ภัยแทน\n" + chat.NO_ANSWER
    bubble = cards.text_messages(Reply(text, emergency=True))[0]["contents"]
    assert bubble["header"]["backgroundColor"] == cards.DANGER
    assert _uris(bubble["body"])[:2] == ["tel:1784", "tel:1669"]
    assert chat.NO_ANSWER in _texts(bubble["body"])


def test_hotlines_are_one_tap_each():
    uris = _uris(cards.text_messages(Reply(chat.HOTLINES)))
    assert {"tel:1784", "tel:1669", "tel:191", "tel:199", cards.LINE_URL} <= set(uris)


def test_too_long_or_empty_stays_plain_text():
    assert cards.text_messages(Reply("ก" * (cards.MAX_CARD_TEXT + 1))) is None
    assert cards.text_messages(Reply("   ")) is None


def test_alert_band_takes_the_tier_colour():
    (msg,) = cards.alert_messages("🟠 เตือนภัยน้ำ — บ้าน\n• ยกของขึ้นที่สูง\n\n" + answer.FOOTER, "warning")
    assert msg["contents"]["header"]["backgroundColor"] == cards.TIER_COLOR["warning"]


def test_every_canned_reply_builds_a_valid_card():
    from line import webhook
    for text in (chat.HELP, chat.NOT_UNDERSTOOD, chat.THANKS, chat.EVAC_TEXT, chat.ALERT_TEXT, chat.CONSENT_PROMPT,
                 webhook.WELCOME + answer.FOOTER, webhook.FALLBACK):
        (msg,) = cards.text_messages(Reply(text))
        assert 0 < len(msg["altText"]) <= 400
        assert all(t.strip() for t in _texts(msg["contents"]))      # LINE rejects empty text components
        for ln in filter(str.strip, text.removesuffix(answer.FOOTER).split("\n")):
            assert any(ln.strip().removeprefix("• ") in t for t in _texts(msg["contents"])), ln
