"""LINE rich replies for a risk answer: a native map pin (like a shared location) plus a Flex card.
Every number comes from the Source objects the agent already built from tool output (rule 1); this only lays them out.
"""
import re

from agent.answer import _forecast, _ts
from agent.risk import TIER_TH

# NamMa design system (namma-web src/styles/tokens.json), light theme: LINE has no dark-mode hook for Flex.
INK, MUTED = "#0B1A2E", "#465870"                              # ink, ink-muted
SURFACE, SUNKEN, LINE_C, LINE_STRONG = "#FFFFFF", "#E2EBF6", "#D0DCEA", "#7889A1"
RIVER, RIVER_SOFT, HOME_SOFT = "#1557B0", "#DDE9F9", "#EAF0F8"
TIER_COLOR = {"normal": "#4D6178", "watch": "#E9B81F", "warning": "#D9600F", "danger": "#A8102F"}
ON_TIER = {"normal": "#FFFFFF", "watch": "#261C00", "warning": "#1F0C00", "danger": "#FFFFFF"}
TIER_SOFT = {"normal": "#E4EAF2", "watch": "#FBF0C9", "warning": "#FBE2CF", "danger": "#F8DDE2"}
TIER_INK = {"normal": "#3A4C61", "watch": "#735600", "warning": "#9A3D00", "danger": "#8F0D27"}
DANGER = TIER_COLOR["danger"]
# Type scale in px (tokens.json "type"): many readers are older, so nothing under the 14 px caption.
BODY_LG, BODY, CAPTION, HEADING, BAND = "18px", "16px", "14px", "19px", "17px"
LINE_URL = "https://line.me/R/ti/p/%401784DDPM"


def _text(t: str, size: str = BODY, color: str = INK, **kw) -> dict:
    """Thai needs room for tone marks above and vowels below: 1.6-1.7 line height, as on the web."""
    return {"type": "text", "text": t, "wrap": True, "size": size, "color": color,
            "lineSpacing": "8px" if size in (BODY_LG, HEADING) else "6px", **kw}


def _btn(label: str, uri: str, kind: str = "secondary") -> dict:
    """A design-system button as a tappable box: 48 px tap target, 10 px radius, 16 px semibold label.
    primary = river fill, danger = tier-danger fill (calls only), secondary = white with a line-strong border."""
    fill, ink = {"primary": (RIVER, "#FFFFFF"), "danger": (DANGER, "#FFFFFF"), "secondary": (SURFACE, INK)}[kind]
    box = {"type": "box", "layout": "vertical", "justifyContent": "center", "height": "48px", "cornerRadius": "10px",
           "backgroundColor": fill, "action": {"type": "uri", "label": label[:40], "uri": uri},
           "contents": [_text(label, BODY, ink, weight="bold", align="center", wrap=False)]}
    if kind == "secondary":
        box |= {"borderWidth": "1px", "borderColor": LINE_STRONG}
    return box


def _call(number: str, label: str) -> dict:
    return _btn(label, f"tel:{number}", "danger")


def _row(label: str, value: str) -> dict:
    return {"type": "box", "layout": "baseline", "spacing": "md", "contents": [
        _text(label, BODY, MUTED, flex=2), _text(value, BODY, INK, flex=4, weight="bold")]}


def _band(lines: list[tuple[str, str]], bg: str, ink: str) -> dict:
    """Header band as the web's alert band (nm-alert__band): 17 px semibold on the tier fill."""
    return {"type": "box", "layout": "vertical", "spacing": "xs", "backgroundColor": bg, "paddingAll": "16px",
            "contents": [_text(t, size, ink, weight="bold") for t, size in lines]}


def _source(t: str, **kw) -> dict:
    return _text(t, CAPTION, MUTED, **kw)


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
        _text(f"{st['code']} {st['name_th']}", HEADING, INK, weight="bold"),
        _source(" • ".join(filter(None, [st.get("river"), f"ห่าง {st['distance_km']:.1f} กม."]))),
        {"type": "separator", "margin": "lg", "color": LINE_C},
        {"type": "box", "layout": "vertical", "spacing": "md", "margin": "lg", "contents": _water_rows(st)},
    ]
    shelters = [s for s in sources if s.get("type") == "shelter"]
    if shelters:
        body += [{"type": "separator", "margin": "lg", "color": LINE_C},
                 _source("ศูนย์พักพิงที่แนะนำ", margin="lg"),
                 _text(f"{shelters[0]['name']} ห่าง {shelters[0]['distance_km']:.1f} กม.", BODY, INK, weight="bold")]
    gistda = [s["summary"] for s in sources if s.get("type") == "gistda"]
    if gistda:
        body += [_source("GISTDA: " + " • ".join(gistda), margin="md")]
    if saved_label:   # household items sit on home-soft (tokens.json "home-soft")
        body += [_panel(f"📌 บันทึกเป็น \"{saved_label}\" แล้ว จะแจ้งเตือนเมื่อน้ำสูงขึ้น", HOME_SOFT, INK, bold=True)]
    body += [{"type": "box", "layout": "vertical", "spacing": "xs", "margin": "lg", "contents": [
        _source(f"ข้อมูล ณ {_ts(st.get('observed_at'))} จาก ThaiWater (สสน.) / {st.get('agency') or '-'}"),
        _source("ค่าที่สถานีริมแม่น้ำ ไม่ใช่ระดับน้ำที่บ้านโดยตรง ใช้เป็นการประเมินเท่านั้น"
                + (" • ตำแหน่งประมาณจากชื่อพื้นที่" if place.get("approx") else "")),
        _source("น้ำมาไม่ใช่หน่วยงานทางการ ติดตามประกาศและทำตามคำสั่งอพยพของ ปภ. เสมอ")]}]
    return {"type": "bubble", "size": "giga",
            "header": _band([(f"📍 {place['label']}", HEADING), (f"ความเสี่ยง: {TIER_TH[tier]}", BAND)],
                            TIER_COLOR[tier], ON_TIER[tier]),
            "body": {"type": "box", "layout": "vertical", "spacing": "sm", "paddingAll": "16px", "contents": body},
            "footer": {"type": "box", "layout": "vertical", "spacing": "sm", "paddingAll": "16px", "paddingTop": "0px",
                       "contents": [_call("1784", "โทร 1784 (ปภ.)"), _btn("เปิดใน Google Maps", gmap)]}}


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

BRAND = "🌊 น้ำมา"
MAX_CARD_TEXT = 3000                     # longer than any canned reply; past it the plain text reads better
_LEAD_EMOJI = re.compile("^[\U0001F000-\U0001FAFF\u2139\u2600-\u27BF\u2B00-\u2BFF][\uFE0F]?\\s*")
_NUMBERED = re.compile(r"^(\d{1,2})\.\s+(.+)")
NOTE_MARKS = ("⚠️", "📢", "ℹ️", "🔒")
DDPM_BUTTONS = [_call("1784", "โทร 1784 (ปภ.)"), _btn("LINE @1784DDPM", LINE_URL)]
DISCLAIMER = "น้ำมาไม่ใช่หน่วยงานทางการ ทำตามประกาศและคำสั่งอพยพของทางการเสมอ"


def _bullet(text: str, mark: str, color: str) -> dict:
    return {"type": "box", "layout": "baseline", "spacing": "md", "contents": [
        _text(mark, BODY_LG, color, flex=0, weight="bold"), _text(text, BODY_LG, INK, flex=1)]}


def _panel(line: str, bg: str, ink: str, bold: bool = False, size: str = BODY) -> dict:
    return {"type": "box", "layout": "vertical", "backgroundColor": bg, "cornerRadius": "4px",
            "paddingAll": "12px", "margin": "md",
            "contents": [_text(line, size, ink, **({"weight": "bold"} if bold else {}))]}


def _note(line: str) -> dict:
    """⚠️ / 📢 / ℹ️ lines: caveats, on surface-sunken like the web's hotline notice. Still body size: they matter."""
    return _panel(line, SUNKEN, INK)


def _urgent(line: str) -> dict:
    """‼️ lines (someone at home cannot walk): the one thing in the card that must not be missed."""
    return _panel(line, TIER_SOFT["danger"], TIER_INK["danger"], bold=True, size=BODY_LG)


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
        return _text(line, HEADING, accent, weight="bold", margin="md")
    return _text(line, BODY_LG, INK)


def _title(text: str) -> tuple[str | None, str]:
    """A short first line led by an emoji ("🏫 ศูนย์พักพิงใกล้…:") is the card's title; the rest is the body."""
    first, _, rest = text.partition("\n")
    if rest.strip() and _LEAD_EMOJI.match(first) and len(first) <= 80 and not first.startswith(NOTE_MARKS):
        return first.rstrip(":"), rest
    return None, text


def text_bubble(text: str, tier: str | None = None, title: str | None = None, emergency: bool = False) -> dict | None:
    """The reply as one bubble, styled like the web's bot bubble: white, river wordmark, 18 px text, paragraphs
    split by hairlines, the DDPM footer as call buttons. tier (alerts): the web's alert band on the tier fill."""
    from agent.answer import FOOTER
    from agent.chat import RESCUE_HOTLINE
    text = text.strip()
    if not text or len(text) > MAX_CARD_TEXT:
        return None
    footer = text.endswith(FOOTER)
    text = text.removesuffix(FOOTER).strip()
    if emergency or text.startswith(RESCUE_HOTLINE):
        emergency, tier = True, "danger"
        text = text.removeprefix(RESCUE_HOTLINE).strip()
        title = title or "🚨 ถ้าอันตรายตอนนี้ โทรทันที"
    accent = TIER_INK[tier] if tier else RIVER
    lead = not emergency and title is None   # untitled: the first line says what the reply is about, so it leads
    if title is None:
        title, text = _title(text)
        lead = lead and title is None
    body: list[dict] = []
    if emergency:   # the calls first, above anything else the reply says
        body += [{"type": "box", "layout": "vertical", "spacing": "sm", "contents": [
            _call("1784", "โทร 1784 (ปภ.)"), _call("1669", "โทร 1669 (เจ็บป่วย)"),
            _btn("LINE @1784DDPM", LINE_URL)]}]
    for para in filter(None, (p.strip() for p in text.split("\n\n"))):
        lines = [ln.strip() for ln in para.split("\n") if ln.strip()]
        rows = []
        if lead:
            rows.append(_text(lines.pop(0), HEADING, INK, weight="bold"))
            lead = False
        rows += [_line(ln, accent) for ln in lines]
        if body:
            body.append({"type": "separator", "margin": "xl", "color": LINE_C})
        body.append({"type": "box", "layout": "vertical", "spacing": "md", "margin": "xl" if body else "none",
                     "contents": rows})
    if not body:
        return None
    if tier:
        header = _band([(title or BRAND, BAND)], TIER_COLOR[tier], ON_TIER[tier])
    else:   # no band: the web's bot bubble is plain white, the wordmark in river
        header = {"type": "box", "layout": "vertical", "paddingAll": "16px", "paddingBottom": "0px", "contents": [
            _text(title or BRAND, HEADING if title else BAND, RIVER, weight="bold"),
            {"type": "separator", "margin": "lg", "color": LINE_C}]}
    bubble = {"type": "bubble", "size": "giga", "header": header,
              "body": {"type": "box", "layout": "vertical", "spacing": "md", "paddingAll": "16px", "contents": body}}
    foot = DDPM_BUTTONS[:] if footer and not emergency else []
    if footer:
        foot.append(_source(DISCLAIMER, align="center", margin="md"))
    if foot:
        bubble["footer"] = {"type": "box", "layout": "vertical", "spacing": "sm", "paddingAll": "16px",
                            "paddingTop": "0px", "contents": foot}
    return bubble


def _alt(text: str) -> str:
    first = next((ln for ln in text.split("\n") if ln.strip()), "น้ำมา")
    return first.strip()[:380]


def flex(bubble: dict, text: str) -> dict:
    return {"type": "flex", "altText": _alt(text), "contents": bubble}


def _hotline_row(label: str, number: str, uri: str) -> dict:
    """As the web's hotline notice: label in ink, the number 22 px semibold danger-ink, underlined; the row is the tap."""
    return {"type": "box", "layout": "horizontal", "alignItems": "center", "height": "48px",
            "action": {"type": "uri", "label": label[:40], "uri": uri}, "contents": [
                _text(label, BODY_LG, INK, flex=1),
                _text(number, "22px" if number.isdigit() else BODY_LG, TIER_INK["danger"], flex=0, weight="bold",
                      align="end", decoration="underline", wrap=False)]}


def hotline_bubble() -> dict:
    """The "which numbers" answer: one tap per number instead of a list to copy from."""
    rows = [("สายด่วน ปภ. (24 ชม.)", "1784", "tel:1784"), ("LINE ปภ.", "@1784DDPM", LINE_URL),
            ("เจ็บป่วยฉุกเฉิน", "1669", "tel:1669"), ("ตำรวจ", "191", "tel:191"), ("ดับเพลิง/กู้ภัย", "199", "tel:199")]
    body = []
    for i, r in enumerate(rows):
        if i:
            body.append({"type": "separator", "color": LINE_C})
        body.append(_hotline_row(*r))
    return {"type": "bubble", "size": "giga",
            "header": {"type": "box", "layout": "vertical", "paddingAll": "16px", "paddingBottom": "0px", "contents": [
                _text("☎️ เบอร์ที่ควรรู้", HEADING, INK, weight="bold"), _source("แตะที่เบอร์เพื่อโทรได้ทันที")]},
            "body": {"type": "box", "layout": "vertical", "paddingAll": "16px", "contents": [
                {"type": "box", "layout": "vertical", "backgroundColor": SUNKEN, "cornerRadius": "4px",
                 "paddingStart": "16px", "paddingEnd": "16px", "paddingTop": "4px", "paddingBottom": "4px",
                 "contents": body}]}}


def text_messages(reply) -> list[dict] | None:
    """Any reply without a station to show: one Flex bubble of its text. None means send the plain text."""
    from agent.chat import HOTLINES
    if reply.text.strip() == HOTLINES:
        return [flex(hotline_bubble(), HOTLINES)]
    bubble = text_bubble(reply.text, emergency=reply.emergency)
    return [flex(bubble, reply.text)] if bubble else None


def alert_messages(text: str, tier: str) -> list[dict] | None:
    """A pushed alert (agent.alerts.alert_text): the web's alert band in the tier's colour."""
    bubble = text_bubble(text, tier=tier if tier in TIER_COLOR else None)
    return [flex(bubble, text)] if bubble else None
