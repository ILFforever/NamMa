"""Read a message with the LLM first: what the person wants, which place, and which household facts they stated.

The rule router (agent/chat.py) used to decide alone, and missed anything its regexes did not foresee: English
("My friend's house is in Chiang Rai Meung district"), typos ("อำเภแเมือง"), unusual phrasing. Now one LLM call sees
the whole message, recent turns, saved places and the rules' own guess (as a hint it may overrule), and returns JSON.
That one call also replaces the separate memory-extraction call, so a turn makes no more LLM calls than before.

The LLM never supplies a number (CLAUDE.md §2 rule 1): it names places in Thai, and agent.geo.resolve looks the
coordinates up in the gazetteer; water levels still come from the tools. With no LLM, a failed call or output
that is not the JSON asked for, read() returns None and the rules decide alone, as before.
"""
import json
import re
from dataclasses import dataclass, field

from agent import llm, memory
from agent.geo import Place, resolve

INTENTS = ("risk", "shelter", "evacuate", "danger", "profile", "hotline", "alerts", "greeting", "thanks",
           "remember", "set_home", "add_place", "other")

SYSTEM = """You read one message sent to "น้ำมา" (NamMa), an unofficial Thai flood assistant on LINE and the web.
NamMa checks live river-gauge data near a place, finds shelters, remembers the household, and sends flood alerts.
People write in Thai or English, with typos, slang and romanised place names. Understand what they mean.

Return JSON only:
{"intent": "<one intent>",
 "place": null or {"label": "<whose place, in Thai: บ้าน / บ้านแม่ / บ้านเพื่อน / บ้านแฟน / ร้าน / ที่ทำงาน>" or null,
                   "province": "<official Thai province name>", "amphoe": "<Thai district name>" or null,
                   "tambon": "<Thai subdistrict name>" or null},
 "saved": "<the saved place label the message refers to>" or null,
 "facts": {<household facts, schema below>},
 "reply": "<only for intent other: a short answer, see below>" or null}

Intents:
- risk: will it flood / is it safe / water level at a place, or about the person's own or a saved place
- shelter: where to go, shelters, evacuation centres, supplies handed out
- evacuate: whether to evacuate, wanting to stay, asking about evacuation orders
- danger: they or someone is in danger NOW (water in the house, trapped, cannot get out, injured, asking for rescue)
- profile: what NamMa remembers about them, or wanting it deleted
- hotline: phone numbers to call
- alerts: asking to be warned / notified
- greeting, thanks
- remember: only telling household facts or where a place is, no question
- set_home / add_place: asking to save or change their home / another place, without saying where
- other: anything else (general flood preparation, how NamMa works, or off-topic)

Places: Thai names exactly as officially written. The เมือง district of a province is "เมือง<province>"
("Chiang Rai Meung", "อำเภอเมือง เชียงราย" → province "เชียงราย", amphoe "เมืองเชียงราย"). Bangkok: province
"กรุงเทพมหานคร", amphoe is the เขต without the word เขต, tambon is the แขวง without the word แขวง. Give only
names the message states or plainly implies; never guess a district that is not mentioned. Use "label" only when the
message says whose place it is ("บ้านแฟนอยู่…", "my friend's house is in…").

Facts (omit what is not stated; {} if nothing):
{"household": {"immobile": [who], "elderly": true, "children": true, "pregnant": true, "pets": [kind]},
 "needs": {"dialysis": true, "oxygen": true, "medication": true, "power_dependent": true},
 "house": {"stories": int, "raised": true, "near_water": true},
 "assets": {"car": true, "motorbike": true, "ground_floor_shop": true},
 "history": {"past_floods": [short Thai text]}}
Only what is literally stated about the person's own household: not negations, other households, hypotheticals
("ถ้า…") or questions. "my mother is 80" is not a health condition.

reply (intent other only): 1-3 short sentences in the language of the message, calm and practical. No numbers
except the hotlines 1784 (DDPM), 1669 (medical), 191 (police), 199 (fire). Never say anyone is safe, never advise
ignoring official evacuation orders. If off-topic, say briefly that NamMa helps with floods.

The rule-based guess you are given is often wrong on English, typos and unusual phrasing: overrule it whenever the
message means something else."""

ALLOWED_NUMBERS = {"1784", "1669", "191", "199"}


@dataclass
class Understanding:
    intent: str
    place: Place | None = None          # resolved in the gazetteer; None when no place or not found
    label: str | None = None            # whose place it is ("บ้านแฟน"), when the message says
    saved: str | None = None            # a saved label the message refers to
    facts: dict = field(default_factory=dict)
    reply: str | None = None            # intent other: a short answer, checked by safe_reply


def read(text: str, hint: str, saved_labels: list[str], recent: list[dict]) -> Understanding | None:
    if not llm.available():
        return None
    convo = "\n".join(f"{m['role']}: {m['content'][:200]}" for m in recent[-6:]) or "-"
    user = (f"Saved places: {', '.join(saved_labels) or '-'}\nRecent conversation:\n{convo}\n"
            f"Rule-based guess: {hint}\n\nMessage: {text}")
    out = llm.chat([{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
                   temperature=0, max_tokens=700, timeout=15)
    return parse(out, text, saved_labels)


def parse(out: str | None, text: str, saved_labels: list[str]) -> Understanding | None:
    m = re.search(r"\{.*\}", out or "", re.S)
    try:
        d = json.loads(m.group(0)) if m else None
    except json.JSONDecodeError:
        d = None
    if not isinstance(d, dict) or d.get("intent") not in INTENTS:
        return None
    u = Understanding(d["intent"])
    p = d.get("place")
    if isinstance(p, dict):
        u.place = resolve(p.get("province"), p.get("amphoe"), p.get("tambon"))
        if isinstance(p.get("label"), str) and 0 < len(p["label"]) <= 20:
            u.label = p["label"].replace(" ", "")
    if d.get("saved") in saved_labels:
        u.saved = d["saved"]
    if isinstance(d.get("facts"), dict):
        u.facts = memory.clean(text, d["facts"])
    if isinstance(d.get("reply"), str):
        u.reply = d["reply"].strip() or None
    return u


def safe_reply(reply: str | None) -> str | None:
    """The LLM's own answer for intent other, only if it keeps rule 1 (no numbers but the hotlines) and rule 3."""
    if not reply or len(reply) > 600:
        return None
    if any(n not in ALLOWED_NUMBERS for n in re.findall(r"\d+(?:[.,]\d+)?", reply)):
        return None
    if re.search(r"ปลอดภัยแน่นอน|ไม่ต้องอพยพ|you are safe|you're safe|no need to evacuate", reply, re.I):
        return None
    return reply
