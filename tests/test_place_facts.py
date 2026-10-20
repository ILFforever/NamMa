"""Facts that belong to one saved place (user_locations.facts): "ร้านส้มตำมีหมา" is the shop's dog, not the home's.
Facts said without a place stay general and hold everywhere; medical needs always stay with the person."""
from datetime import timedelta

from agent import alerts, chat, risk, tools
from tests.test_db_flow import NOW, UP, _set_over_bank, conn  # noqa: F401  (pytest fixture)
from tests.test_understand import fake_llm  # noqa: F401  (pytest fixture)

PET_TIP = "สายจูงสัตว์เลี้ยง"


def _user(conn, ext):  # noqa: F811
    """A home and a shop, both near the test stations (Bang Ban)."""
    uid = tools.get_or_create_user(conn, ext)
    tools.save_location(conn, uid, "บ้าน", 14.41, 100.46)
    tools.save_location(conn, uid, "ร้านส้มตำ", 14.42, 100.46)
    return uid


def _loc(conn, uid, label):  # noqa: F811
    return next(l for l in tools.get_user_profile(conn, uid)["locations"] if l["label"] == label)


def test_a_fact_about_a_saved_place_is_kept_there(conn, fake_llm):  # noqa: F811
    risk.compute_all(conn, NOW)
    uid = _user(conn, "U-pf1")
    msg = "ร้านส้มตำมีหมา"
    fake_llm[msg] = {"intent": "remember", "saved": "ร้านส้มตำ", "facts": {"household": {"pets": ["หมา"]}}}
    r = chat.handle(conn, "U-pf1", "web", text=msg)
    assert "(ร้านส้มตำ)" in r.text and "ขออนุญาตจำ" not in r.text    # a pet is not health data: no consent asked
    prof = tools.get_user_profile(conn, uid)
    assert _loc(conn, uid, "ร้านส้มตำ")["facts"] == {"household": {"pets": ["หมา"]}}
    assert _loc(conn, uid, "ร้านส้มตำ")["pending"] == {} and prof["memory"]["household"] == {}

    # The dog shows up for the shop, not for the home.
    shop = chat.handle(conn, "U-pf1", "web", text="ร้านส้มตำน้ำจะท่วมไหม")
    home = chat.handle(conn, "U-pf1", "web", text="บ้านน้ำจะท่วมไหม")
    assert PET_TIP in shop.text and PET_TIP not in home.text


def test_rules_alone_keep_a_place_fact_there(conn):  # noqa: F811
    uid = _user(conn, "U-pf2")
    chat.handle(conn, "U-pf2", "line", text="ร้านส้มตำมีหมา")
    assert _loc(conn, uid, "ร้านส้มตำ")["facts"] == {"household": {"pets": ["หมา"]}}
    assert _loc(conn, uid, "บ้าน")["facts"] == {} and tools.get_user_profile(conn, uid)["memory"]["household"] == {}


def test_a_fact_without_a_place_is_general(conn):  # noqa: F811
    risk.compute_all(conn, NOW)
    uid = _user(conn, "U-pf3")
    chat.handle(conn, "U-pf3", "line", text="มีหมา")
    assert tools.get_user_profile(conn, uid)["memory"]["household"] == {"pets": ["หมา"]}
    for q in ("ร้านส้มตำน้ำจะท่วมไหม", "บ้านน้ำจะท่วมไหม"):
        assert PET_TIP in chat.handle(conn, "U-pf3", "line", text=q).text


def test_medical_needs_stay_with_the_person(conn):  # noqa: F811
    uid = _user(conn, "U-pf4")
    chat.handle(conn, "U-pf4", "line", text="ร้านส้มตำมีหมา แม่ต้องฟอกไต")
    assert _loc(conn, uid, "ร้านส้มตำ")["facts"] == {"household": {"pets": ["หมา"]}}
    assert tools.get_user_profile(conn, uid)["pending"] == {"needs": {"dialysis": True}}


def test_a_place_saved_with_a_fact_keeps_it(conn):  # noqa: F811
    uid = tools.get_or_create_user(conn, "U-pf5")
    chat.handle(conn, "U-pf5", "line", text="บ้านแม่อยู่บางบาล อยุธยา บ้านสองชั้น")
    assert _loc(conn, uid, "บ้านแม่")["facts"] == {"house": {"stories": 2}}
    assert tools.get_user_profile(conn, uid)["memory"]["house"] == {}


def test_a_saved_other_household_is_not_someone_else(conn):  # noqa: F811
    """"บ้านเพื่อน…ติดเตียง" is another household, unless บ้านเพื่อน is a saved place: then it is that place's."""
    uid = tools.get_or_create_user(conn, "U-pf6")
    tools.save_location(conn, uid, "บ้านเพื่อน", 14.41, 100.46)
    chat.handle(conn, "U-pf6", "line", text="บ้านเพื่อนมียายติดเตียง")
    assert _loc(conn, uid, "บ้านเพื่อน")["pending"] == {"household": {"immobile": ["ยาย"]}}
    chat.handle(conn, "U-pf6", "line", text="ข้างบ้านมีตาติดเตียง")       # a neighbour: nothing kept
    assert _loc(conn, uid, "บ้านเพื่อน")["pending"] == {"household": {"immobile": ["ยาย"]}}
    assert tools.get_user_profile(conn, uid)["pending"] == {}


def test_alerts_use_the_place_facts(conn):  # noqa: F811
    risk.compute_all(conn, NOW)
    uid = _user(conn, "U-pf7")
    chat.handle(conn, "U-pf7", "line", text="บ้านมียายติดเตียง")
    chat.handle(conn, "U-pf7", "line", text=chat.CONSENT_YES)
    assert _loc(conn, uid, "บ้าน")["facts"] == {"household": {"immobile": ["ยาย"]}}
    _set_over_bank(conn, UP, 0.2)
    sent = alerts.run(conn, NOW + timedelta(minutes=3))
    assert len(sent) == 2
    texts = {r[0].split("—", 1)[1].split("\n", 1)[0].strip(): r[0] for r in conn.execute(
        "SELECT content FROM messages WHERE channel = 'alert' AND user_id = %s", (uid,)).fetchall()}
    assert "เดินไม่ได้" in texts["บ้าน"] and "เดินไม่ได้" not in texts["ร้านส้มตำ"]


def test_forgetting_health_clears_every_place(conn):  # noqa: F811
    uid = _user(conn, "U-pf8")
    chat.handle(conn, "U-pf8", "line", text="บ้านมียายติดเตียง")
    chat.handle(conn, "U-pf8", "line", text=chat.CONSENT_YES)
    chat.handle(conn, "U-pf8", "line", text="ร้านส้มตำมียายติดเตียง")
    r = chat.handle(conn, "U-pf8", "line", text="ลบข้อมูลสุขภาพ")
    assert r.text.startswith("ลบข้อมูลสุขภาพ")
    assert _loc(conn, uid, "บ้าน")["facts"] == {} and _loc(conn, uid, "ร้านส้มตำ")["pending"] == {}


def test_consent_no_drops_place_facts_too(conn):  # noqa: F811
    uid = _user(conn, "U-pf9")
    chat.handle(conn, "U-pf9", "line", text="ร้านส้มตำมียายติดเตียง")
    chat.handle(conn, "U-pf9", "line", text=chat.CONSENT_NO)
    assert _loc(conn, uid, "ร้านส้มตำ")["pending"] == {} and _loc(conn, uid, "ร้านส้มตำ")["facts"] == {}


def test_merging_accounts_joins_a_place_facts(conn):  # noqa: F811
    a, b = tools.get_or_create_user(conn, "web:anon"), tools.get_or_create_user(conn, "U-pf10")
    la = tools.save_location(conn, a, "ร้าน", 14.41, 100.46)
    lb = tools.save_location(conn, b, "ร้าน", 14.40, 100.46)
    tools.merge_place_facts(conn, la, {"household": {"pets": ["แมว"]}})
    tools.merge_place_facts(conn, lb, {"household": {"elderly": True}, "house": {"stories": 1}})
    tools.merge_users(conn, a, b)
    [loc] = tools.get_user_profile(conn, b)["locations"]
    assert loc["facts"] == {"household": {"pets": ["แมว"], "elderly": True}, "house": {"stories": 1}}


def test_a_pet_needs_no_consent_but_health_still_does(conn):  # noqa: F811
    """A pet is not health data: kept at once, no PDPA prompt. Said with a bedridden grandma, only she waits."""
    uid = tools.get_or_create_user(conn, "U-pf11")
    r = chat.handle(conn, "U-pf11", "line", text="มีแมว 2 ตัว")
    assert "ขออนุญาตจำ" not in r.text and "ข้อมูลสุขภาพ" not in r.text
    assert tools.get_user_profile(conn, uid)["memory"]["household"] == {"pets": ["แมว"]}
    r = chat.handle(conn, "U-pf11", "line", text="มีหมา ยายติดเตียง")
    prof = tools.get_user_profile(conn, uid)
    assert "ขออนุญาตจำ" in r.text
    assert prof["memory"]["household"] == {"pets": ["หมา"]} and prof["pending"] == {"household": {"immobile": ["ยาย"]}}
