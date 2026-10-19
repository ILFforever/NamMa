import base64
import hashlib
import hmac
from datetime import datetime, timedelta

from agent import alerts, answer, llm, memory, triage
from agent.geo import find_place, haversine_km
from ingest.thaiwater import BKK


def test_haversine():
    assert round(haversine_km(13.75, 100.5, 14.35, 100.57)) == 67


def test_find_place_demo_phrases():
    p = find_place("บ้านแม่อยู่บางระกำ พิษณุโลก น้ำจะถึงไหม")
    assert p.province == "พิษณุโลก" and p.amphoe == "บางระกำ"
    p = find_place("บ้านอยู่บางบาล อยุธยา")
    assert p.province == "พระนครศรีอยุธยา" and p.amphoe == "บางบาล"
    assert find_place("สวัสดีค่ะ") is None


def test_memory_rules_stated_only():
    f = memory.extract_rules("ยายติดเตียง บ้านชั้นเดียว ติดคลอง มีรถยนต์")
    assert f["household"]["immobile"] == ["ยาย"]
    assert f["house"] == {"stories": 1, "near_water": True}
    assert f["assets"] == {"car": True}
    assert memory.extract_rules("แม่อายุ 80 แล้ว") == {}         # age is not a health condition
    plain, sensitive = memory.split_sensitive(f)
    assert "household" in sensitive and "household" not in plain


def test_location_label():
    assert memory.location_label("บ้านแม่อยู่บางระกำ") == "บ้านแม่"
    assert memory.location_label("บ้านผมอยู่เสนา") == "บ้าน"
    assert memory.location_label("น้ำจะท่วมไหม") is None


def test_triage():
    assert triage.is_danger("น้ำเข้าบ้านแล้ว ยายเดินไม่ได้")
    assert not triage.is_danger("น้ำจะถึงไหม")
    d = triage.call_details("น้ำเข้าบ้านแล้ว ยายเดินไม่ได้ อยู่กัน 4 คน ขอเรือ", {})
    assert d["people_count"] == 4 and any("ยาย" in v for v in d["vulnerable"])
    d = triage.call_details("ช่วยด้วย น้ำเข้าบ้าน", {"household": {"immobile": ["ตา"]}, "needs": {"oxygen": True}})
    assert d == {"people_count": None, "vulnerable": ["ตา (เดินไม่ได้/ติดเตียง)", "ใช้ออกซิเจน"]}   # from memory
    assert triage.call_details("ช่วยด้วย", {}) == {"people_count": None, "vulnerable": []}


def test_gistda_lines():
    extent = {"days": 3, "at_point": False, "nearest_km": 1.2, "area_km2_nearby": 2.5, "radius_km": 5.0,
              "as_of": "2026-10-18T13:00:00+07:00"}
    assert answer.flood_extent_text(extent, approx=False) == \
        "ภาพดาวเทียม GISTDA 3 วันล่าสุด: ไม่พบน้ำท่วมที่จุดนี้ พื้นที่น้ำท่วมใกล้สุดห่างราว 1.2 กม. (ข้อมูล ณ 13:00 น. 18/10)"
    # Around a place name the point is only a district centre: the area around, not a yes/no.
    assert "รัศมี 5 กม. รวมราว 1,562 ไร่" in answer.flood_extent_text(extent, approx=True)
    assert "พบน้ำท่วมที่จุดนี้" in answer.flood_extent_text({**extent, "at_point": True, "nearest_km": 0.0}, False)
    assert "ไม่พบพื้นที่น้ำท่วมในรัศมี 5 กม." in answer.flood_extent_text({**extent, "nearest_km": None}, True)
    assert answer.recurrence_text({"tambon": "บ้านกุ่ม", "years": [2011, 2017, 2022]}) == \
        "สถิติ GISTDA 2011–2023: ต.บ้านกุ่ม มีน้ำท่วม 3 ปี (ล่าสุดปี 2022)"
    assert answer.recurrence_text({"years": []}) is None
    txt = answer.risk_answer("บ้านแม่", False, [STATION], [], {}, extent, {"tambon": None, "years": [2011]})
    assert "🛰️ ภาพดาวเทียม GISTDA 3 วันล่าสุด" in txt and "🛰️ สถิติ GISTDA 2011–2023: พื้นที่นี้" in txt


def test_numbers_guard():
    assert llm.numbers_preserved("ระดับ 4.65 ม. ห่าง 3.2 กม.", "น้ำสูง 4.65 ม.")
    assert not llm.numbers_preserved("ระดับ 4.65 ม.", "อีก 2 ชม. น้ำถึง")


class _Resp:
    def __init__(self, status, content=None):
        self.status_code, self._content = status, content

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


def test_llm_chula_thinking_off_and_fallbacks(monkeypatch):
    monkeypatch.setenv("MAAS_BASE_URL", "https://llm.example/v1")
    monkeypatch.setenv("MAAS_API_KEY", "k")
    monkeypatch.setenv("MAAS_MODEL", "qwen3.5")
    monkeypatch.setenv("LLM_NO_THINKING", "1")
    sent = {}

    def ok(url, json, **kw):
        sent.update(json)
        return _Resp(200, "<think>\n</think>\n\nสวัสดีค่ะ")
    monkeypatch.setattr(llm.httpx, "post", ok)
    assert llm.chat([{"role": "user", "content": "hi"}]) == "สวัสดีค่ะ"   # think block stripped
    assert sent["chat_template_kwargs"] == {"enable_thinking": False} and sent["model"] == "qwen3.5"

    monkeypatch.setattr(llm.httpx, "post", lambda *a, **kw: _Resp(429))
    assert llm.chat([{"role": "user", "content": "hi"}]) is None          # quota used up → rules

    monkeypatch.delenv("LLM_NO_THINKING")
    assert "chat_template_kwargs" not in llm.payload([], 0.3, 10)         # other servers: plain body


STATION = {"station_id": 2609, "code": "C.35", "name_th": "บ้านป้อม", "river": "แม่น้ำเจ้าพระยา", "agency": "RID",
           "distance_km": 3.2, "reading_ts": "2026-10-14T15:00:00+07:00", "level_msl": 4.9, "diff_wl_bank": 0.55,
           "situation_level": 5, "trend_3h_cm_per_hr": 1.2, "hours_to_bank": None, "risk_tier": "danger",
           "upstream": [{"oldcode": "C.7A", "name_th": "บ้านบางแก้ว", "risk_tier": "danger", "approx_travel_hours": 10}]}


def test_risk_answer_names_source_and_1784():
    txt = answer.risk_answer("บ้านแม่ อ.บางบาล", True, [STATION], [], {"household": {"immobile": ["ยาย"]}})
    assert "C.35 บ้านป้อม" in txt and "3.2 กม." in txt and "สูงกว่าตลิ่ง 0.55 ม." in txt
    assert "1784" in txt and "@1784DDPM" in txt and "ไม่ใช่ระดับน้ำที่บ้านโดยตรง" in txt
    assert "ยาย" in txt and "C.7A" in txt


def test_alert_decide():
    now = datetime(2026, 10, 14, 12, tzinfo=BKK)
    assert alerts.decide("watch", None, None, now, False) == "push"
    assert alerts.decide("normal", None, None, now, False) is None
    assert alerts.decide("warning", "watch", now, now, False) == "push"
    assert alerts.decide("warning", "warning", now, now, False) is None
    assert alerts.decide("danger", "danger", now - timedelta(hours=7), now, False) == "push"
    assert alerts.decide("danger", "danger", now - timedelta(hours=2), now, False) is None
    assert alerts.decide("watch", "danger", now, now, False) == "record"
    assert alerts.decide("warning", "watch", now, now, True) is None     # quiet hours hold non-danger
    assert alerts.decide("danger", "watch", now, now, True) == "push"
    assert alerts.in_quiet_hours({"quiet_hours": [22, 6]}, now.replace(hour=23))
    assert not alerts.in_quiet_hours({"quiet_hours": [22, 6]}, now)


def test_line_signature(monkeypatch):
    from line import client
    monkeypatch.setenv("LINE_CHANNEL_SECRET", "s3cret")
    body = b'{"events":[]}'
    sig = base64.b64encode(hmac.new(b"s3cret", body, hashlib.sha256).digest()).decode()
    assert client.verify_signature(body, sig)
    assert not client.verify_signature(body, "nope")
