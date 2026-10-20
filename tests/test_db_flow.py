"""End-to-end flow against a real Postgres: risk → chat → consent → alerts → danger → delete.

Uses TEST_DATABASE_URL (default: the local dev server, database namma_test, recreated each run).
Skipped when no Postgres is reachable. Network calls (ThaiWater, GISTDA, MaaS, LINE) are disabled.
"""
import os
from datetime import datetime, timedelta

import psycopg
import pytest

from agent import alerts, answer, chat, risk, tools
from common.config import ROOT

TEST_URL = os.environ.get("TEST_DATABASE_URL", "postgresql://namma:namma@localhost:5433/namma_test")
ADMIN_URL = TEST_URL.rsplit("/", 1)[0] + "/postgres"
DB_NAME = TEST_URL.rsplit("/", 1)[1]


@pytest.fixture()
def conn(monkeypatch):
    try:
        admin = psycopg.connect(ADMIN_URL, autocommit=True, connect_timeout=3)
    except psycopg.OperationalError:
        pytest.skip("no Postgres for integration tests")
    admin.execute(f"DROP DATABASE IF EXISTS {DB_NAME} WITH (FORCE)")
    admin.execute(f"CREATE DATABASE {DB_NAME}")
    admin.close()
    for k in ("MAAS_API_KEY", "GISTDA_API_KEY", "LINE_CHANNEL_SECRET", "LINE_CHANNEL_ACCESS_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(tools, "_fill_history", lambda conn, ids: False)
    c = psycopg.connect(TEST_URL)
    for path in sorted((ROOT / "infra" / "migrations").glob("*.sql")):
        c.execute(path.read_text())
    _seed(c)
    c.commit()
    yield c
    c.close()


NOW = datetime.now().astimezone().replace(second=0, microsecond=0)
DOWN, UP = 9001, 9002            # fake ids, near Bang Ban (Ayutthaya)


def _seed(c):
    for sid, code, lat in ((DOWN, "T.1", 14.41), (UP, "T.2", 14.60)):
        c.execute("INSERT INTO stations (id, oldcode, name_th, lat, lon, river, agency) VALUES (%s, %s, %s, %s, 100.46, "
                  "'แม่น้ำทดสอบ', 'RID')", (sid, code, f"สถานี{code}", lat))
    c.execute("INSERT INTO station_edges VALUES (%s, %s, 10)", (UP, DOWN))
    c.execute("INSERT INTO shelters (name, lat, lon, accepts_patients) VALUES ('ศูนย์ทดสอบ', 14.40, 100.47, true)")
    for h in range(4):
        ts = NOW - timedelta(hours=3 - h)
        c.execute("INSERT INTO waterlevel_readings (station_id, ts, level_msl, diff_wl_bank) VALUES (%s, %s, %s, %s)",
                  (DOWN, ts, 4.0 + 0.01 * h, -1.5 + 0.01 * h))         # rising 1 cm/h, 1.47 m below bank
        c.execute("INSERT INTO waterlevel_readings (station_id, ts, level_msl, diff_wl_bank) VALUES (%s, %s, %s, %s)",
                  (UP, ts, 9.0, -0.3))                                # upstream steady, below bank


def _set_over_bank(c, sid, diff):
    c.execute("INSERT INTO waterlevel_readings (station_id, ts, level_msl, diff_wl_bank) VALUES (%s, %s, 9.5, %s) "
              "ON CONFLICT (station_id, ts) DO UPDATE SET diff_wl_bank = EXCLUDED.diff_wl_bank",
              (sid, NOW + timedelta(minutes=1), diff))
    risk.compute_all(c, NOW + timedelta(minutes=2))
    c.commit()


def test_full_flow(conn):
    res = {r.station_id: r.risk_tier for r in risk.compute_all(conn, NOW)}
    assert res == {DOWN: "normal", UP: "normal"}

    r = chat.handle(conn, "U-test", "line", text="บ้านแม่อยู่บางบาล อยุธยา น้ำจะถึงไหม")
    assert "T.1" in r.text and "1784" in r.text and r.text.endswith(answer.FOOTER)
    assert "บ้านแม่" in r.text and "ผู้สูงอายุ" in r.text              # saved note + household question
    prof = tools.get_user_profile(conn, tools.get_or_create_user(conn, "U-test"))
    assert [l["label"] for l in prof["locations"]] == ["บ้านแม่"]

    r = chat.handle(conn, "U-test", "line", text="ยายติดเตียง")
    assert [q.text for q in r.quick] == [chat.CONSENT_YES, chat.CONSENT_NO]
    uid = tools.get_or_create_user(conn, "U-test")
    assert tools.get_user_profile(conn, uid)["memory"]["household"] == {}   # nothing stored before consent
    chat.handle(conn, "U-test", "line", text=chat.CONSENT_YES)
    assert tools.get_user_profile(conn, uid)["memory"]["household"] == {"immobile": ["ยาย"]}

    # Normal tier → no alert. Upstream over bank → warning → one personalised push, then dedupe.
    assert alerts.run(conn, NOW) == []
    _set_over_bank(conn, UP, 0.2)
    sent = alerts.run(conn, NOW + timedelta(minutes=3))
    assert [s["tier"] for s in sent] == ["warning"]
    msg = conn.execute("SELECT content FROM messages WHERE channel = 'alert' ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert "เดินไม่ได้" in msg and "ศูนย์ทดสอบ" in msg and "1784" in msg
    assert alerts.run(conn, NOW + timedelta(minutes=4)) == []

    _set_over_bank(conn, DOWN, 0.1)                                         # escalation to danger pushes again
    assert [s["tier"] for s in alerts.run(conn, NOW + timedelta(minutes=5))] == ["danger"]

    # Danger: 1784 first, then what to tell them, from the message and memory. Nothing is filed anywhere.
    r = chat.handle(conn, "U-test", "line", text="น้ำเข้าบ้านแล้ว ยายเดินไม่ได้ อยู่กัน 3 คน")
    assert r.text.startswith("🚨") and "1784" in r.text.splitlines()[0] and r.emergency
    assert "ไม่ได้ส่งเรื่อง" in r.text and "ตอนโทร แจ้งว่า:" in r.text
    assert "ยาย (เดินไม่ได้/ติดเตียง)" in r.text and "จำนวนคน: 3" in r.text
    assert "• ตำแหน่ง: บ้านแม่ (อ.บางบาล" in r.text              # the saved place, in words 1784 can use
    assert r.quick == []
    assert conn.execute("SELECT to_regclass('rescue_tickets')").fetchone()[0] is None

    chat.handle(conn, "U-test", "line", text="ลบข้อมูลทั้งหมด")
    prof = tools.get_user_profile(conn, uid)
    assert prof["locations"] == [] and prof["memory"]["household"] == {}


def test_danger_with_nothing_known_is_the_hotline_and_what_to_do(conn):
    r = chat.handle(conn, "U-sos", "line", text="ช่วยด้วย ติดอยู่บนหลังคา")
    assert r.emergency and r.text.startswith(chat.RESCUE_HOTLINE)
    assert "ตอนโทร" not in r.text and "ระหว่างรอ:" in r.text


def test_a_gps_place_gives_coordinates_to_tell_1784(conn):
    risk.compute_all(conn, NOW)
    r = chat.handle(conn, "U-gps", "web", location=(14.4321, 100.4567))
    assert not r.emergency and "T.1" in r.text                    # a location share is a normal risk check
    r = chat.handle(conn, "U-gps", "web", text="น้ำเข้าบ้านแล้ว ช่วยด้วย")
    assert r.emergency and "• ตำแหน่ง: บ้าน (พิกัด 14.43210, 100.45670)" in r.text
    # A place named in the message beats the saved one.
    r = chat.handle(conn, "U-gps", "web", text="ช่วยด้วย น้ำเข้าบ้าน บางบาล อยุธยา")
    assert "• ตำแหน่ง: อ.บางบาล จ.พระนครศรีอยุธยา" in r.text


def test_demo_endpoints_need_a_session_and_demo_mode(api, monkeypatch):
    from api import main
    from tests.conftest import login

    c, _ = api
    assert c.post("/api/demo/simulate", json={"station_id": 1}).status_code == 401
    assert c.post("/api/demo/simulate", json={"station_id": 1}, headers={"Authorization": "Bearer wrong"}).status_code == 401
    user = {"Authorization": f"Bearer {login(c)}"}
    assert c.post("/api/demo/simulate", json={"station_id": 1}, headers=user).status_code == 404   # no risk row
    assert c.post("/api/demo/refresh", headers=user).status_code == 200
    monkeypatch.setattr(main, "DEMO", False)
    for path in ("/api/demo/simulate", "/api/demo/refresh"):
        assert c.post(path, json={"station_id": 1}, headers=user).status_code == 403


def test_web_replies_drop_hotline_copies_and_emoji_line_keeps_them(conn):
    risk.compute_all(conn, NOW)
    q = "บ้านอยู่บางบาล อยุธยา น้ำจะถึงไหม"
    line, web = chat.handle(conn, "U-line", "line", text=q), chat.handle(conn, "web:s1", "web", text=q)
    assert line.text.endswith(answer.FOOTER) and "📡" in line.text
    assert "1784" not in web.text and not chat._EMOJI.search(web.text) and "T.1" in web.text
    assert web.text.replace("\n", "") == chat._EMOJI.sub("", web.text).replace("\n", "")

    sos = "ช่วยด้วย ติดอยู่บนหลังคา"
    line, web = chat.handle(conn, "U-line", "line", text=sos), chat.handle(conn, "web:s1", "web", text=sos)
    assert line.text.startswith(chat.RESCUE_HOTLINE)
    assert chat.RESCUE_HOTLINE not in web.text and web.text.startswith("น้ำมาไม่ได้ส่งเรื่อง") and web.emergency
    # The logged assistant message is what the person saw
    uid = tools.get_or_create_user(conn, "web:s1", None)
    assert conn.execute("SELECT content FROM messages WHERE user_id = %s AND role = 'assistant' "
                        "ORDER BY id DESC LIMIT 1", (uid,)).fetchone()[0] == web.text

    # No station source → no HotlineNotice on the web, so the footer text stays (emoji removed)
    help_ = chat.handle(conn, "web:s1", "web", text="สวัสดี")
    assert "1784" in help_.text and not chat._EMOJI.search(help_.text)


def _locs(conn, uid):
    return {l["label"]: (l["lat"], l["lon"]) for l in tools.get_user_profile(conn, uid)["locations"]}


def test_shared_location_is_saved_as_home_and_profile_can_edit(conn):
    risk.compute_all(conn, NOW)
    r = chat.handle(conn, "U-home", "line", location=(14.41, 100.46))
    uid = tools.get_or_create_user(conn, "U-home", None)
    assert _locs(conn, uid) == {chat.HOME: (14.41, 100.46)}
    assert r.place["saved_label"] == chat.HOME and r.place["lat"] == 14.41
    # the profile view is always editable
    p = chat.handle(conn, "U-home", "line", text="ข้อมูลของฉัน")
    assert {chat.SET_HOME, chat.ADD_PLACE, chat.ADD_FACTS} <= {q.text for q in p.quick}
    assert chat.HOME in p.text
    # edit the home: the next share replaces it, still one home
    s = chat.handle(conn, "U-home", "line", text=chat.SET_HOME)
    assert any(q.kind == "location" for q in s.quick)
    chat.handle(conn, "U-home", "line", location=(14.5, 100.5))
    assert _locs(conn, uid) == {chat.HOME: (14.5, 100.5)}
    # add another place: name first, then the location
    chat.handle(conn, "U-home", "line", text=chat.ADD_PLACE)
    n = chat.handle(conn, "U-home", "line", text="บ้านแม่")
    assert "บ้านแม่" in n.text and any(q.kind == "location" for q in n.quick)
    chat.handle(conn, "U-home", "line", location=(15.0, 100.0))
    assert _locs(conn, uid) == {chat.HOME: (14.5, 100.5), "บ้านแม่": (15.0, 100.0)}
    # a later share with no pending edit does not overwrite the home
    chat.handle(conn, "U-home", "line", location=(13.0, 100.0))
    assert _locs(conn, uid)[chat.HOME] == (14.5, 100.5) and "ตำแหน่งที่แชร์" in _locs(conn, uid)


def test_empty_profile_offers_to_add_and_bare_home_starts_saving(conn):
    risk.compute_all(conn, NOW)
    p = chat.handle(conn, "U-new", "line", text="ข้อมูลของฉัน")
    assert "ยังไม่ได้จำ" in p.text and chat.SET_HOME in [q.text for q in p.quick]
    b = chat.handle(conn, "U-new", "line", text="บ้าน")
    assert any(q.kind == "location" for q in b.quick)
    chat.handle(conn, "U-new", "line", location=(14.41, 100.46))
    uid = tools.get_or_create_user(conn, "U-new", None)
    assert _locs(conn, uid) == {chat.HOME: (14.41, 100.46)}
    again = chat.handle(conn, "U-new", "line", text="บ้าน")      # now it assesses the saved home
    assert again.place and again.place["label"] == chat.HOME


def test_rescue_words_are_not_taken_as_a_place_name(conn):
    chat.handle(conn, "U-safe", "line", text=chat.ADD_PLACE)
    r = chat.handle(conn, "U-safe", "line", text="ช่วยด้วย")
    assert r.emergency and r.text.startswith(chat.RESCUE_HOTLINE)
    chat.handle(conn, "U-safe", "line", location=(14.41, 100.46))
    assert "ช่วยด้วย" not in _locs(conn, tools.get_or_create_user(conn, "U-safe", None))


def test_unplaced_risk_question_asks_which_place_never_greets(conn):
    risk.compute_all(conn, NOW)
    r = chat.handle(conn, "U-doi", "web", text="บ้านอยู่ตีนดอยสุเทพ จะเป็นอะไรไหมอะ")
    assert not r.text.startswith("สวัสดี") and "ต.สุเทพ อ.เมืองเชียงใหม่ จ.เชียงใหม่" in r.text
    assert "น้ำป่า" in r.text and "1784" in r.text                 # hillside: say what stations cannot see
    pick = r.quick[0].text
    assert pick == "บ้านอยู่ ต.สุเทพ อ.เมืองเชียงใหม่ จ.เชียงใหม่ น้ำจะถึงไหม"
    a = chat.handle(conn, "U-doi", "web", text=pick)              # the quick reply places it and saves the home
    assert a.text.startswith("บ้าน ต.สุเทพ") and "ไม่พบสถานีวัดน้ำ" in a.text   # test stations are all in Ayutthaya
    assert "บ้าน" in _locs(conn, tools.get_or_create_user(conn, "U-doi", None))


def test_unplaced_place_is_not_answered_with_another_saved_place(conn):
    risk.compute_all(conn, NOW)
    chat.handle(conn, "U-two", "line", text="บ้านอยู่บางบาล อยุธยา น้ำจะถึงไหม")
    r = chat.handle(conn, "U-two", "line", text="บ้านแม่อยู่ตีนดอยสุเทพ จะเป็นอะไรไหม")
    assert not r.sources and "ต.สุเทพ" in r.text
    # A concern question with no flood word still checks the saved home
    r = chat.handle(conn, "U-two", "line", text="บ้านจะเป็นอะไรไหม")
    assert r.sources and r.sources[0]["code"] == "T.1"


def test_only_a_greeting_gets_the_greeting(conn):
    assert chat.handle(conn, "U-g", "line", text="สวัสดีครับ").text.startswith("สวัสดีค่ะ")
    other = chat.handle(conn, "U-g", "line", text="กินข้าวยัง")
    assert other.text.startswith("น้ำมายังไม่เข้าใจ") and "1784" in other.text
    assert "ยังหาตำแหน่งนี้ไม่เจอ" in chat.handle(conn, "U-g", "line", text="ปลอดภัยไหม").text


def test_alert_pushes_only_to_real_line_ids(conn, monkeypatch):
    # A demo stand-in counts as a LINE account for sign-in, but LINE rejects it as a push target ("to" invalid).
    # It must get the alert in its chat history, like a web-only user, instead of failing every run.
    monkeypatch.setenv("LINE_CHANNEL_SECRET", "s")
    monkeypatch.setenv("LINE_CHANNEL_ACCESS_TOKEN", "t")
    pushed = []
    monkeypatch.setattr(alerts.line, "push", lambda to, msgs: pushed.append(to))
    real = "U" + "0123456789abcdef" * 2
    for ext in (real, "demo:responder"):
        tools.save_location(conn, tools.get_or_create_user(conn, ext), "บ้าน", 14.41, 100.46)
    _set_over_bank(conn, UP, 0.2)
    assert len(alerts.run(conn, NOW + timedelta(minutes=3))) == 2
    assert pushed == [real]
    demo = tools.get_or_create_user(conn, "demo:responder")
    assert conn.execute("SELECT count(*) FROM messages WHERE user_id = %s AND channel = 'alert'", (demo,)).fetchone()[0] == 1
    assert alerts.run(conn, NOW + timedelta(minutes=4)) == []                # recorded, so no retry every run


def test_station_page_message_pins_alerts_to_that_station(conn):
    # "แจ้งเตือนสถานี T.1 #9001" is what the web's station page fills in for LINE (namma-web copy.station.lineAlert).
    near = 9003                                                              # 4 km from T.1, no edge to it
    conn.execute("INSERT INTO stations (id, oldcode, name_th, lat, lon, river, agency) "
                 "VALUES (%s, 'T.3', 'สถานีT.3', 14.45, 100.46, 'แม่น้ำทดสอบ', 'RID')", (near,))
    for h in range(4):
        conn.execute("INSERT INTO waterlevel_readings (station_id, ts, level_msl, diff_wl_bank) VALUES (%s, %s, 6.0, -1.0)",
                     (near, NOW - timedelta(hours=3 - h)))
    risk.compute_all(conn, NOW)
    assert chat.handle(conn, "U-st", "line", text="แจ้งเตือนสถานี #123").text.startswith("ไม่พบสถานี")

    r = chat.handle(conn, "U-st", "line", text=f"แจ้งเตือนสถานี T.1 #{DOWN}")
    assert r.text.startswith("📍 สถานี T.1") and "บันทึก \"สถานี T.1\" ไว้แล้ว" in r.text and "1784" in r.text
    assert r.place["saved_label"] == "สถานี T.1" and r.sources[0]["station_id"] == DOWN
    uid = tools.get_or_create_user(conn, "U-st")
    assert [(l["label"], l["station_id"]) for l in tools.get_user_profile(conn, uid)["locations"]] == [("สถานี T.1", DOWN)]

    # A plain place at the same point hears of the riskier station nearby; the pinned one waits for its own station.
    tools.save_location(conn, tools.get_or_create_user(conn, "U-plain"), "บ้าน", 14.41, 100.46)
    _set_over_bank(conn, near, 0.3)
    assert [(s["station"], s["tier"]) for s in alerts.run(conn, NOW + timedelta(minutes=3))] == [("T.3", "danger")]
    _set_over_bank(conn, DOWN, 0.1)
    assert [(s["station"], s["tier"]) for s in alerts.run(conn, NOW + timedelta(minutes=4))] == [("T.1", "danger")]

    # Saving the same label again from a GPS share or a place name drops the pin.
    tools.save_location(conn, uid, "สถานี T.1", 14.41, 100.46)
    assert tools.get_user_profile(conn, uid)["locations"][0]["station_id"] is None
