"""LINE rich replies for a risk answer: a native map pin (like a shared location) plus a Flex card.
Every number comes from the Source objects the agent already built from tool output (rule 1); this only lays them out.
"""
import re

from agent.answer import _forecast, _ts
from agent.risk import TIER_TH

TIER_COLOR = {"normal": "#2F9E44", "watch": "#F59F00", "warning": "#F76707", "danger": "#E03131"}
INK, MUTED = "#10264F", "#5B6B86"
LINE_URL = "https://line.me/R/ti/p/%401784DDPM"


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
    if forecast := _forecast(st):       # one horizon per line, as the level row splits its two parts
        rows.append(_row("แบบจำลองคาดการณ์", forecast.replace(" · ", "\n")))
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
        _text(" • ".join(filter(None, [st.get("river"), f"ห่าง {st['distance_km']:.1f} กม."])), size="xs", color=MUTED),
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
        extra = "\n\n".join(reply.extra)
        bubble = text_bubble(extra)
        msgs.append(flex(bubble, extra) if bubble else {"type": "text", "text": extra})
    return msgs


# ---- Every other reply: the same text, laid out as a Flex bubble instead of a plain text message. ----
# Nothing is added or reworded here: the text the agent built is split into a title, sections, bullet rows and notes,
# and the DDPM footer becomes tap-to-call buttons. A reply the layout cannot take falls back to plain text.

RIVER, RIVER_SOFT, DANGER = "#1557B0", "#EAF2FC", TIER_COLOR["danger"]
LINE_GREEN = "#06C755"
BRAND = "🌊 น้ำมา NamMa"
MAX_CARD_TEXT = 3000                     # longer than any canned reply; past it the plain text reads better
_LEAD_EMOJI = re.compile("^[\U0001F000-\U0001FAFFℹ☀-➿⬀-⯿][️]?\\s*")
_NUMBERED = re.compile(r"^(\d{1,2})\.\s+(.+)")
NOTE_MARKS = ("⚠️", "📢", "ℹ️", "🔒")


def _uri_button(label: str, uri: str, color: str | None = None, style: str = "secondary") -> dict:
    b = {"type": "button", "style": style, "height": "sm", "action": {"type": "uri", "label": label[:20], "uri": uri}}
    if color:
        b["color"] = color
    return b


def _call(number: str, label: str, color: str = DANGER) -> dict:
    return _uri_button(label, f"tel:{number}", color, "primary")


DDPM_BUTTONS = [_call("1784", "โทร 1784 (ปภ.)"), _uri_button("LINE @1784DDPM", LINE_URL, LINE_GREEN, "primary")]


def _bullet(text: str, mark: str, color: str) -> dict:
    return {"type": "box", "layout": "baseline", "spacing": "sm", "contents": [
        _text(mark, size="sm", color=color, flex=0, weight="bold"), _text(text, size="sm", color=INK, flex=1)]}


def _note(line: str) -> dict:
    """⚠️ / 📢 / ℹ️ lines: caveats, so a quieter tinted box instead of body text."""
    return {"type": "box", "layout": "vertical", "backgroundColor": "#F4F6FA", "cornerRadius": "md",
            "paddingAll": "10px", "margin": "md", "contents": [_text(line, size="xs", color=MUTED)]}


def _urgent(line: str) -> dict:
    """‼️ lines (someone at home cannot walk): the one thing in the card that must not be missed."""
    return {"type": "box", "layout": "vertical", "backgroundColor": "#FFF0F0", "cornerRadius": "md",
            "paddingAll": "10px", "margin": "md", "contents": [_text(line, size="sm", color=DANGER, weight="bold")]}


def _line(line: str, accent: str) -> dict:
    if line.startswith("‼️"):
        return _urgent(line)
    if line.startswith("• "):
        return _bullet(line[2:], "•", accent)
    if m := _NUMBERED.match(line):
        return _bullet(m.group(2), f"{m.group(1)}.", accent)
    if line.startswith(NOTE_MARKS):
        return _note(line)
    if line.startswith("✅") or (line.endswith(":") and len(line) <= 60):
        return _text(line, size="sm", weight="bold", color=accent, margin="md")
    return _text(line, size="sm", color=INK)


def _title(text: str) -> tuple[str | None, str]:
    """A short first line led by an emoji ("🏫 ศูนย์พักพิงใกล้…:") is the card's title; the rest is the body."""
    first, _, rest = text.partition("\n")
    if rest.strip() and _LEAD_EMOJI.match(first) and len(first) <= 80 and not first.startswith(NOTE_MARKS):
        return first.rstrip(":"), rest
    return None, text


def text_bubble(text: str, accent: str = RIVER, title: str | None = None, buttons: list[dict] | None = None,
                emergency: bool = False) -> dict | None:
    """The reply as one bubble: coloured title band, paragraphs split by separators, DDPM footer as buttons."""
    from agent.answer import FOOTER
    from agent.chat import RESCUE_HOTLINE
    text = text.strip()
    if not text or len(text) > MAX_CARD_TEXT:
        return None
    footer = text.endswith(FOOTER)
    text = text.removesuffix(FOOTER).strip()
    if emergency or text.startswith(RESCUE_HOTLINE):
        emergency, accent = True, DANGER
        text = text.removeprefix(RESCUE_HOTLINE).strip()
        title = title or "🚨 ถ้าอันตรายตอนนี้ โทรทันที"
    lead = not emergency and title is None   # untitled: the first line says what the reply is about, so it leads
    if title is None:
        title, text = _title(text)
        lead = lead and title is None
    body: list[dict] = []
    if emergency:   # the calls first, above anything else the reply says
        body += [{"type": "box", "layout": "vertical", "spacing": "sm", "contents": [
            _call("1784", "โทร 1784 (ปภ.)"), _call("1669", "โทร 1669 (เจ็บป่วย)")]},
            _text("หรือทัก LINE @1784DDPM", size="xs", color=MUTED, align="center")]
    for para in filter(None, (p.strip() for p in text.split("\n\n"))):
        lines = [ln.strip() for ln in para.split("\n") if ln.strip()]
        rows = []
        if lead:
            rows.append(_text(lines.pop(0), size="md", color=INK, weight="bold"))
            lead = False
        rows += [_line(ln, accent) for ln in lines]
        if body:
            body.append({"type": "separator", "margin": "lg"})
        body.append({"type": "box", "layout": "vertical", "spacing": "sm", "margin": "lg" if body else "none",
                     "contents": rows})
    if not body:
        return None
    bubble = {"type": "bubble", "size": "mega",
              "header": {"type": "box", "layout": "vertical", "backgroundColor": accent, "paddingAll": "14px",
                         "contents": [_text(title or BRAND, color="#FFFFFF", weight="bold", size="md")]},
              "body": {"type": "box", "layout": "vertical", "spacing": "sm", "contents": body}}
    foot = list(buttons or [])
    if footer and not emergency:
        foot += DDPM_BUTTONS
    if footer:
        foot.append(_text("น้ำมาไม่ใช่หน่วยงานทางการ ทำตามประกาศและคำสั่งอพยพของทางการเสมอ", size="xxs",
                          color=MUTED, align="center", margin="sm"))
    if foot:
        bubble["footer"] = {"type": "box", "layout": "vertical", "spacing": "sm", "contents": foot}
    return bubble


def _alt(text: str) -> str:
    first = next((ln for ln in text.split("\n") if ln.strip()), "น้ำมา")
    return first.strip()[:380]


def flex(bubble: dict, text: str) -> dict:
    return {"type": "flex", "altText": _alt(text), "contents": bubble}


def hotline_bubble() -> dict:
    """The "which numbers" answer: one tap per number instead of a list to copy from."""
    return {"type": "bubble", "size": "mega",
            "header": {"type": "box", "layout": "vertical", "backgroundColor": DANGER, "paddingAll": "14px",
                       "contents": [_text("☎️ เบอร์ที่ควรรู้", color="#FFFFFF", weight="bold", size="md"),
                                    _text("แตะเพื่อโทรได้ทันที", color="#FFFFFF", size="xs")]},
            "body": {"type": "box", "layout": "vertical", "spacing": "sm", "contents": [
                _call("1784", "สายด่วน ปภ. 1784"), _uri_button("LINE @1784DDPM", LINE_URL, LINE_GREEN, "primary"),
                _call("1669", "เจ็บป่วยฉุกเฉิน 1669"),
                {"type": "box", "layout": "horizontal", "spacing": "sm", "contents": [
                    _uri_button("ตำรวจ 191", "tel:191"), _uri_button("ดับเพลิง 199", "tel:199")]},
                _text("สายด่วน ปภ. เปิด 24 ชม.", size="xxs", color=MUTED, align="center", margin="md")]}}


def text_messages(reply) -> list[dict] | None:
    """Any reply without a station to show: one Flex bubble of its text. None means send the plain text."""
    from agent.chat import HOTLINES
    if reply.text.strip() == HOTLINES:
        return [flex(hotline_bubble(), HOTLINES)]
    bubble = text_bubble(reply.text, emergency=reply.emergency)
    return [flex(bubble, reply.text)] if bubble else None


def alert_messages(text: str, tier: str) -> list[dict] | None:
    """A pushed alert (agent.alerts.alert_text): the band takes the tier's colour."""
    bubble = text_bubble(text, accent=TIER_COLOR.get(tier, RIVER))
    return [flex(bubble, text)] if bubble else None
