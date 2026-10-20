"""LLM-first reading of a message (agent/understand.py) with a stand-in LLM: the cases the rules alone got wrong."""
import json

import pytest

from agent import chat, llm, risk, understand
from tests.test_db_flow import NOW, conn  # noqa: F401  (pytest fixture)


@pytest.fixture()
def fake_llm(monkeypatch):
    """Answers the understanding call with the JSON queued for the message; every other LLM call fails (rules)."""
    answers = {}

    def chat_(messages, **kw):
        if messages[0]["content"] != understand.SYSTEM:
            return None
        msg = messages[1]["content"].rsplit("Message: ", 1)[1]
        out = answers.get(msg)
        return out if isinstance(out, str) else json.dumps(out) if out else None

    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(llm, "chat", chat_)
    return answers


def test_english_place_with_romanised_district(conn, fake_llm):  # noqa: F811
    risk.compute_all(conn, NOW)
    msg = "My friend's house is in Chiang Rai Meung district"
    fake_llm[msg] = {"intent": "risk", "place": {"label": "บ้านเพื่อน", "province": "เชียงราย", "amphoe": "เมืองเชียงราย"}}
    r = chat.handle(conn, "U-en", "line", text=msg)
    assert "อ.เมืองเชียงราย จ.เชียงราย" in r.text and "บันทึก \"บ้านเพื่อน\"" in r.text
    loc = conn.execute("SELECT label FROM user_locations").fetchone()
    assert loc[0] == "บ้านเพื่อน"


def test_typo_the_rules_cannot_read(conn, fake_llm):  # noqa: F811
    msg = "บ้านแฟนอยู่เชียงราย อำเภแเมือง ท่วมไหม"
    fake_llm[msg] = {"intent": "risk", "place": {"label": "บ้านแฟน", "province": "เชียงราย", "amphoe": "เมือง"}}
    r = chat.handle(conn, "U-typo", "web", text=msg)
    assert "อ.เมืองเชียงราย" in r.text and "กว้างเกิน" not in r.text


def test_llm_coordinates_are_never_used(conn, fake_llm):  # noqa: F811
    """A place the gazetteer does not know is not placed, whatever the LLM says."""
    msg = "บ้านอยู่ Narnia น้ำท่วมไหม"
    fake_llm[msg] = {"intent": "risk", "place": {"province": "Narnia", "amphoe": "x", "lat": 13.7, "lon": 100.5}}
    r = chat.handle(conn, "U-x", "web", text=msg)
    assert r.text.startswith("ยังหาตำแหน่งนี้ไม่เจอ") and not r.sources


def test_llm_can_raise_danger_but_never_lower_it(conn, fake_llm):  # noqa: F811
    msg = "water is coming in and grandma can't get up the stairs"
    fake_llm[msg] = {"intent": "danger"}
    assert chat.handle(conn, "U-d", "line", text=msg).emergency
    msg = "น้ำเข้าบ้านแล้ว ยายเดินไม่ได้"
    fake_llm[msg] = {"intent": "greeting"}           # a wrong reading must not hide a danger message
    assert chat.handle(conn, "U-d2", "line", text=msg).emergency


def test_other_reply_keeps_the_rules(conn, fake_llm):  # noqa: F811
    ok = "how do I prepare a go-bag?"
    fake_llm[ok] = {"intent": "other", "reply": "Pack water, medicine, a torch and documents in a waterproof bag. Call 1784 for help."}
    r = chat.handle(conn, "U-o", "web", text=ok)
    assert r.text.startswith("Pack water") and "1784" in r.text
    bad = "will the water rise tomorrow?"
    fake_llm[bad] = {"intent": "other", "reply": "It will rise 30 cm tomorrow, you are safe."}
    r = chat.handle(conn, "U-o", "web", text=bad)
    assert "30 cm" not in r.text and "you are safe" not in r.text


def test_bad_llm_output_falls_back_to_rules(conn, fake_llm):  # noqa: F811
    msg = "บ้านอยู่บางบาล อยุธยา น้ำจะถึงไหม"
    fake_llm[msg] = "sorry, I can't help with that"
    risk.compute_all(conn, NOW)
    r = chat.handle(conn, "U-bad", "web", text=msg)
    assert "อ.บางบาล" in r.text


def test_facts_come_from_the_same_call(conn, fake_llm):  # noqa: F811
    msg = "my grandma is bedridden"
    fake_llm[msg] = {"intent": "remember", "facts": {"household": {"immobile": ["ยาย"]}}}
    r = chat.handle(conn, "U-f", "line", text=msg)
    assert "ขออนุญาตจำ" in r.text                  # sensitive: consent first, same as the rules path


def test_parse_rejects_unknown_intent():
    assert understand.parse('{"intent": "launch_rockets"}', "x", []) is None
    assert understand.parse("not json", "x", []) is None
    u = understand.parse('{"intent": "risk", "place": {"province": "กรุงเทพ", "tambon": "ลุมพินี"}, "saved": "ร้าน"}',
                         "x", ["บ้าน"])
    assert u.place.tambon == "ลุมพินี" and u.saved is None


def _shop(conn, ext):  # noqa: F811
    """A user with "ร้านส้มตำ" saved in Mueang Chiang Rai (from its name) and a home in Bang Ban."""
    from agent import tools
    uid = tools.get_or_create_user(conn, ext)
    tools.save_location(conn, uid, "บ้าน", 14.41, 100.46, notes=chat.APPROX_NOTE + "อ.บางบาล จ.พระนครศรีอยุธยา")
    tools.save_location(conn, uid, "ร้านส้มตำ", 19.91, 99.84, notes=chat.APPROX_NOTE + "อ.เมืองเชียงราย จ.เชียงราย")
    return uid


def _labels(conn, uid):  # noqa: F811
    return sorted(r[0] for r in conn.execute("SELECT label FROM user_locations WHERE user_id = %s", (uid,)))


def test_saved_place_by_name_is_not_saved_again(conn, fake_llm):  # noqa: F811
    """"ร้านส้มตำมีหมา": the LLM names the saved place but also guesses its area under a generic label "ร้าน".
    That is the saved ร้านส้มตำ, not a new place called "ร้าน"."""
    risk.compute_all(conn, NOW)
    uid = _shop(conn, "U-shop")
    msg = "ร้านส้มตำมีหมา"
    fake_llm[msg] = {"intent": "risk", "saved": "ร้านส้มตำ",
                     "place": {"label": "ร้าน", "province": "เชียงราย", "amphoe": "เมืองเชียงราย"},
                     "facts": {"household": {"pets": ["หมา"]}}}
    r = chat.handle(conn, "U-shop", "web", text=msg)
    assert _labels(conn, uid) == ["บ้าน", "ร้านส้มตำ"]
    assert r.place and r.place["label"] == "ร้านส้มตำ" and "บันทึก \"ร้าน\"" not in r.text


def test_saved_place_by_name_without_llm(conn):  # noqa: F811
    """The rules alone: "ร้านส้มตำมีหมา จะท่วมไหม" is the saved ร้านส้มตำ, not an unsaved "ร้านส้มตำมีหมา"."""
    risk.compute_all(conn, NOW)
    uid = _shop(conn, "U-shop2")
    r = chat.handle(conn, "U-shop2", "web", text="ร้านส้มตำมีหมา จะท่วมไหม")
    assert r.place and r.place["label"] == "ร้านส้มตำ" and "ยังไม่ได้บันทึก" not in r.text
    assert _labels(conn, uid) == ["บ้าน", "ร้านส้มตำ"]


def test_saved_place_given_a_new_area_moves(conn, fake_llm):  # noqa: F811
    """Saying where a saved place is still moves it: "ร้านส้มตำอยู่บางบาล" is the same label, a new point."""
    risk.compute_all(conn, NOW)
    uid = _shop(conn, "U-shop3")
    msg = "ร้านส้มตำอยู่บางบาล อยุธยา"
    fake_llm[msg] = {"intent": "remember", "saved": "ร้านส้มตำ",
                     "place": {"label": "ร้านส้มตำ", "province": "พระนครศรีอยุธยา", "amphoe": "บางบาล"}}
    chat.handle(conn, "U-shop3", "web", text=msg)
    assert _labels(conn, uid) == ["บ้าน", "ร้านส้มตำ"]
    lat = conn.execute("SELECT lat FROM user_locations WHERE user_id = %s AND label = 'ร้านส้มตำ'", (uid,)).fetchone()[0]
    assert lat < 15


def test_saved_places_reach_the_llm_with_their_area(conn, fake_llm, monkeypatch):  # noqa: F811
    seen = {}
    real = llm.chat
    monkeypatch.setattr(llm, "chat", lambda messages, **kw: seen.setdefault("user", messages[1]["content"]) and real(messages, **kw))
    _shop(conn, "U-shop4")
    chat.handle(conn, "U-shop4", "web", text="สวัสดี")
    assert "ร้านส้มตำ (อ.เมืองเชียงราย จ.เชียงราย)" in seen["user"]


def _place_labels(conn, ext):  # noqa: F811
    from agent import tools
    uid = tools.get_or_create_user(conn, ext)
    return sorted(r[0] for r in conn.execute("SELECT label FROM user_locations WHERE user_id = %s", (uid,)))


def test_add_place_that_says_where_is_saved_there(conn, fake_llm):  # noqa: F811
    """Live, 2026-10-20: read as add_place with label "ร้าน", it asked for a GPS share of "ร้าน" and saved nothing."""
    risk.compute_all(conn, NOW)
    msg = "ร้านส้มตำอยู่อำเภอเมืองเชียงราย"
    fake_llm[msg] = {"intent": "add_place",
                     "place": {"label": "ร้าน", "province": "เชียงราย", "amphoe": "เมืองเชียงราย"}}
    r = chat.handle(conn, "U-add1", "web", text=msg)
    assert "กดแชร์ตำแหน่ง" not in r.text and "อ.เมืองเชียงราย" in r.text
    assert _place_labels(conn, "U-add1") == ["ร้านส้มตำ"]


def test_set_home_that_says_where_is_saved_there(conn, fake_llm):  # noqa: F811
    msg = "บ้านอยู่บางบาล อยุธยา"
    fake_llm[msg] = {"intent": "set_home", "place": {"label": "บ้าน", "province": "พระนครศรีอยุธยา", "amphoe": "บางบาล"}}
    r = chat.handle(conn, "U-add2", "web", text=msg)
    assert "กดแชร์ตำแหน่ง" not in r.text and _place_labels(conn, "U-add2") == ["บ้าน"]


def test_add_place_without_where_keeps_the_typed_name(conn, fake_llm):  # noqa: F811
    msg = "อยากเพิ่มร้านส้มตำ"
    fake_llm[msg] = {"intent": "add_place", "place": {"label": "ร้าน", "province": None}}
    r = chat.handle(conn, "U-add3", "web", text=msg)
    assert "\"ร้านส้มตำ\"" in r.text and "กดแชร์ตำแหน่ง" in r.text
