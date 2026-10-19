"""LINE rich replies for a risk answer: a native map pin (like a shared location) plus a Flex card.
Every number comes from the Source objects the agent already built from tool output (rule 1); this only lays them out.
"""
from agent.answer import _ts
from agent.risk import TIER_TH

TIER_COLOR = {"normal": "#2F9E44", "watch": "#F59F00", "warning": "#F76707", "danger": "#E03131"}
INK, MUTED = "#10264F", "#5B6B86"


def _text(t: str, **kw) -> dict:
    return {"type": "text", "text": t, "wrap": True, **kw}


def _row(label: str, value: str) -> dict:
    return {"type": "box", "layout": "baseline", "spacing": "sm", "contents": [
        _text(label, size="sm", color=MUTED, flex=2), _text(value, size="sm", color=INK, flex=5, weight="bold")]}


def _station(sources: list[dict]) -> dict | None:
    return next((s for s in sources if s.get("type") == "station"), None)


def location_message(place: dict, sources: list[dict]) -> dict:
    st = _station(sources)
    addr = f"สถานีใกล้สุด {st['code']} {st['name_th']} ห่าง {st['distance_km']:.1f} กม." if st else "ตำแหน่งที่บันทึก"
    return {"type": "location", "title": place["label"][:100], "address": addr[:100],
            "latitude": place["lat"], "longitude": place["lon"]}


def _water_rows(st: dict) -> list[dict]:
    rows = []
    lvl, diff = st.get("level_msl"), st.get("diff_wl_bank")
    if lvl is not None:
        v = f"{lvl:.2f} ม.รทก."
        if diff is not None:
            v += f"\n{'สูงกว่าตลิ่ง' if diff >= 0 else 'ต่ำกว่าตลิ่ง'} {abs(diff):.2f} ม."
        rows.append(_row("ระดับน้ำ", v))
    t = st.get("trend_cm_per_hr")
    if t is None:
        rows.append(_row("แนวโน้ม", "ข้อมูลยังไม่พอ"))
    elif t >= 0.5:
        rows.append(_row("3 ชม.ล่าสุด", f"น้ำขึ้น {t:.1f} ซม./ชม."))
    elif t <= -0.5:
        rows.append(_row("3 ชม.ล่าสุด", f"น้ำลง {abs(t):.1f} ซม./ชม."))
    else:
        rows.append(_row("3 ชม.ล่าสุด", "ทรงตัว"))
    if st.get("hours_to_bank") is not None:
        rows.append(_row("ถึงตลิ่ง", f"ราว {st['hours_to_bank']:.0f} ชม. (ประมาณการ)"))
    if st.get("upstream_over"):
        rows.append(_row("ต้นน้ำ", f"{', '.join(st['upstream_over'])} ล้นตลิ่งแล้ว"))
    return rows


def risk_card(place: dict, sources: list[dict], saved_label: str | None) -> dict:
    st = _station(sources)
    tier = st["risk_tier"]
    gmap = f"https://www.google.com/maps/search/?api=1&query={place['lat']},{place['lon']}"
    body = [
        _text(f"{st['code']} {st['name_th']}", weight="bold", size="md", color=INK),
        _text(f"{st.get('river') or 'ไม่ระบุลำน้ำ'} • ห่าง {st['distance_km']:.1f} กม.", size="xs", color=MUTED),
        {"type": "separator", "margin": "md"},
        {"type": "box", "layout": "vertical", "spacing": "sm", "margin": "md", "contents": _water_rows(st)},
    ]
    shelters = [s for s in sources if s.get("type") == "shelter"]
    if shelters:
        body += [{"type": "separator", "margin": "md"},
                 _text("ศูนย์พักพิงที่แนะนำ", size="xs", color=MUTED, margin="md"),
                 _text(f"{shelters[0]['name']} ห่าง {shelters[0]['distance_km']:.1f} กม.", size="sm", color=INK,
                       weight="bold")]
    gistda = [s["summary"] for s in sources if s.get("type") == "gistda"]
    if gistda:
        body += [_text("GISTDA: " + " • ".join(gistda), size="xs", color=MUTED, margin="md")]
    if saved_label:
        body += [_text(f"📌 บันทึกเป็น \"{saved_label}\" แล้ว จะแจ้งเตือนเมื่อน้ำสูงขึ้น", size="xs", color="#0C8F8F",
                       margin="md", weight="bold")]
    body += [_text(f"ข้อมูล ณ {_ts(st.get('observed_at'))} จาก ThaiWater (สสน.) / {st.get('agency') or '-'}",
                   size="xxs", color=MUTED, margin="md"),
             _text("ค่าที่สถานีริมแม่น้ำ ไม่ใช่ระดับน้ำที่บ้านโดยตรง ใช้เป็นการประเมินเท่านั้น"
                   + (" • ตำแหน่งประมาณจากชื่อพื้นที่" if place.get("approx") else ""), size="xxs", color=MUTED),
             _text("น้ำมาไม่ใช่หน่วยงานทางการ ติดตามประกาศและทำตามคำสั่งอพยพของ ปภ. เสมอ", size="xxs", color=MUTED)]
    return {"type": "bubble", "size": "mega",
            "header": {"type": "box", "layout": "vertical", "backgroundColor": TIER_COLOR[tier], "paddingAll": "16px",
                       "contents": [_text(f"📍 {place['label']}", color="#FFFFFF", weight="bold", size="lg"),
                                    _text(f"ความเสี่ยง: {TIER_TH[tier]}", color="#FFFFFF", size="sm")]},
            "body": {"type": "box", "layout": "vertical", "spacing": "xs", "contents": body},
            "footer": {"type": "box", "layout": "vertical", "spacing": "sm", "contents": [
                {"type": "button", "style": "primary", "color": TIER_COLOR[tier], "height": "sm",
                 "action": {"type": "uri", "label": "เปิดใน Google Maps", "uri": gmap}},
                {"type": "button", "style": "secondary", "height": "sm",
                 "action": {"type": "uri", "label": "โทร 1784 (ปภ.)", "uri": "tel:1784"}}]}}


def risk_messages(reply) -> list[dict] | None:
    """[map pin, Flex card, optional text of the extra notes]; None when this reply has no station to show."""
    if not reply.place or not _station(reply.sources):
        return None
    place, saved = reply.place, reply.place.get("saved_label")
    st = _station(reply.sources)
    msgs = [location_message(place, reply.sources),
            {"type": "flex", "altText": f"{place['label']}: ความเสี่ยง {TIER_TH[st['risk_tier']]} ที่สถานี {st['code']} "
                                         "โทร 1784 (ปภ.)",
             "contents": risk_card(place, reply.sources, saved)}]
    if reply.extra:
        msgs.append({"type": "text", "text": "\n\n".join(reply.extra)})
    return msgs
