"""One turn of conversation, channel-agnostic. LINE and the web app both call handle().

Flow (CLAUDE.md §6, manual tool flow so it works with or without MaaS function calling):
classify intent in code → call tools → template facts (+ optional LLM advice) → reply.
"""
import re
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime

from agent import answer, memory, tools, triage, understand
from agent.text import clean
from agent.geo import PROVINCE_ALIASES, find_place, place_candidates, place_text
from agent.risk import rank

CONSENT_YES, CONSENT_NO = "ยินยอมให้จำข้อมูล", "ไม่ยินยอม"
FORGET_ALL_TEXT = "ลบข้อมูลทั้งหมด"  # same text as the profile quick reply (web postback memory:delete_all)
APPROX_NOTE = "ประมาณจากชื่อพื้นที่: "  # user_locations.notes of a place geocoded from its name, not a GPS share
CONSENT_PROMPT = ("🔒 ขออนุญาตจำข้อมูลนี้ เพื่อแจ้งเตือนและช่วยเหลือได้เร็วขึ้นไหม?\n"
                  "ข้อมูลสุขภาพเป็นข้อมูลอ่อนไหวตาม PDPA ถ้าไม่ยินยอม จะใช้เฉพาะในบทสนทนานี้")
_ABOUT = ("น้ำมาช่วยดูความเสี่ยงน้ำท่วมจากข้อมูลสถานีวัดน้ำจริง\n"
          "ลองพิมพ์ เช่น \"บ้านแม่อยู่บางบาล อยุธยา น้ำจะถึงไหม\" หรือกดแชร์ตำแหน่ง\n"
          "ถ้าอยู่ในอันตราย โทร 1784 (ปภ.) ได้ทันที\n\n" + answer.FOOTER)
HELP = "สวัสดีค่ะ " + _ABOUT                              # only for a greeting
NOT_UNDERSTOOD = "น้ำมายังไม่เข้าใจข้อความนี้ค่ะ " + _ABOUT   # anything else nothing matched: never answered with a greeting
GREETING_RE = re.compile(r"^(สวัสดี|หวัดดี|ดีค่ะ|ดีครับ|ดีจ้า|เริ่ม|ช่วยอะไรได้|ทำอะไรได้|คุณคือใคร|ใช้ยังไง|"
                         r"(hello|hi|hey|start|good ?(morning|afternoon|evening))\b|おはよう|こんにちは)", re.I)
THANKS_RE = re.compile(r"^(ขอบคุณ|ขอบใจ|thx|thanks|thank you|ลาก่อน|บาย|bye)", re.I)
THANKS = "ยินดีค่ะ ถ้าอยากให้เช็คอีก พิมพ์ชื่อพื้นที่หรือกดแชร์ตำแหน่งได้ตลอด\nถ้าอยู่ในอันตราย โทร 1784 (ปภ.) ได้ทันที"
# "Where do I go": shelters, evacuation centres and what they hand out. Checked after danger, so
# "ติดอยู่ ไปศูนย์พักพิงไม่ได้" is still danger.
SHELTER_RE = re.compile(r"ศูนย์พัก|ที่พักพิง|ศูนย์อพยพ|จุดอพยพ|อพยพไปที่ไหน|อพยพไปไหน|ไปพักที่ไหน|ที่หลบภัย|"
                        r"ไปนอนที่|ไปนอนไหน|นอนที่วัด|ที่พักฉุกเฉิน|ที่พักชั่วคราว|ที่พักกับ|หลบน้ำ|หนีไปไหน|"
                        r"หนีไปที่ไหน|ไปไหนดี|ไปที่ไหนดี|ควรไปไหน|ต้องไปไหน|ต้องไปที่ไหน|ไปอยู่ที่ไหน|ย้ายไปอยู่ที่ไหน|"
                        r"ศูนย์\S{0,6}ใกล้|ศูนย์ไหน|ศูนย์ที่ไหน|ที่ศูนย์|ศูนย์รับ|นมผง|ที่จอด\S{0,12}ศูนย์|(มี)?ที่(ไหน)?รับ(สัตว์|คน|ผู้|เด็ก)|ขอยา|ยาหมด|"
                        r"ชาร์จ(มือถือ|แบต|โทรศัพท์)|แจก(อาหาร|น้ำ|ของ)|(น้ำดื่ม|อาหาร)\S{0,8}(ที่ไหน|แจก)|"
                        r"เรือ\S{0,10}(ไปส่ง|รับ)|รถรับส่ง|ไปศูนย์|ถุงยังชีพ|ต้องการ(น้ำดื่ม|อาหาร|น้ำ)|ขอ(น้ำ|อาหาร|ข้าว)|ของแจก|"
                        r"ศูนย์\S{0,20}(อยู่ไหน|อยู่ที่ไหน)|ที่(ให้|รับ)\S{0,6}สัตว์|shelter|evacuation cent|where (to|do i|should i) (go|evacuate)",
                        re.I)
# A flood question: flood verbs and compounds, not every word with น้ำ in it ("น้ำจิ้ม", "ดื่มน้ำ").
FLOOD_RE = re.compile(r"น้ำ(จะ|กำลัง|ยัง|มัน)?(ท่วม|ขึ้น|ลด|มา|เข้า|ถึง|ล้น|ป่า|หลาก|ไหล|สูง|เอ่อ|หนุน|ซึม|ทะลัก)|"
                      r"ท่วม|ระดับน้ำ|ตลิ่ง|อพยพ|ฝนตก|ฝน(หนัก|ลง|ไม่หยุด)|พายุ|ดีเปรสชัน|มรสุม|ไซโคลน|เอ่อล้น|"
                      r"ดินถล่ม|ดินสไลด์|ดินโคลน|ดินพัง|เขื่อน\S{0,30}?(ปล่อย|แตก|ระบาย)|"
                      r"เสี่ยง\S{0,6}(ท่วม|น้ำ)|จม(น้ำ)?(ไหม|มั้ย|แน่|หรือเปล่า|รึเปล่า)|โดนน้ำ|ระวังน้ำ|รอระบาย|"
                      r"ท่อระบาย|ฝน\S{0,6}(ตก|หนัก)\S{0,12}(ไหม|มั้ย|หรือเปล่า)|"
                      r"(ร้าน|สต๊อก|สินค้า|นา|ข้าว|ถนน|ตลาด|โรงเรียน|รถ|บ้าน)\S{0,12}(เสียหาย|ขาด|ปิด|จม)\S{0,6}(ไหม|มั้ย|หรือเปล่า)|"
                      r"น้ำทะเลหนุน|ทะเลหนุน|คลื่นซัด|storm surge|ระบายน้ำ|อ่างเก็บน้ำ|ประตูน้ำ|เขื่อน\S{0,30}?(น้ำ|เพิ่ม|ลด)|"
                      r"เสียงตามสาย|ประกาศ\S{0,12}(เตรียม|ระวัง)|น้ำ\S{0,18}(สูงขึ้น|ขึ้นเยอะ|เพิ่มขึ้น)|ขึ้นที่สูง|"
                      r"ยกของ|เก็บของ\S{0,6}(ขึ้น|หนี)|ปิดเรียน|ปิดโรงเรียน|ฉับพลัน|ไหลหลาก|น้ำ\S{0,8}?(เป็นไง|เป็นยังไง|เป็นอย่างไร)|สถานการณ์น้ำ|"
                      r"flood|water level|landslide|storm|nam ?ja|tuam|tueng|thuam", re.I)
# Words and idioms with น้ำ/ท่วม/ฝน… that are not about floods: removed before looking for a risk question.
NOT_FLOOD = re.compile(r"น้ำตา\S{0,4}ท่วม\S{0,4}|น้ำหอม|น้ำใจ|น้ำตา|น้ำผลไม้|น้ำมัน|น้ำปลา|น้ำแข็ง|หิวน้ำ|น้ำตก|อาบน้ำ|ห้องน้ำ|น้ำเสียง|"
                       r"น้ำหนัก|ท่อน้ำ|น้ำประปา|ตลาดน้ำ|น้ำท่วมปาก|น้ำท่วมทุ่ง|ท่วมท้น|น้ำชา|น้ำอัดลม|น้ำเปล่า|"
                       r"น้ำจิ้ม|น้ำข้น|น้ำแกง|ดื่มน้ำ|เล่นน้ำ|ว่ายน้ำ|ตู้ปลา|ดอยคำ|ไซเบอร์|ต่อสายตา|ข้อสอบ|หนัง|"
                       r"เกม|ระดับความ|ฝนดาวตก|ฝนตกพรำ|ฝนตกรถติด|ฝนตกนิดหน่อย|ฝนปรอย|ฝนตกปรอย|พายุอารมณ์|"
                       r"ไม่ได้เกี่ยวกับน้ำท่วม|(?<!ทำ)งานท่วม|ท่วมหัว|ท่วมท้น|"
                       r"น้ำมา(?=รับ|ช่วย|คือ|ทำ|บอก|ตอบ|เป็น|แม่น|ใช้|จำ|ไม่|ครับ|คะ|ค่ะ|$)|น้ำตา\S{0,4}ท่วม\S{0,4}|ตกใจ|"
                       r"ท่อตัน\S{0,10}|น้ำท่วมห้องน้ำ|ค่าน้ำ")
# Clearly about something else ("ปลอดภัยไซเบอร์", "เสี่ยงลงทุน", "ฉากน้ำท่วมในหนัง"): not a risk question.
OFF_TOPIC = re.compile(r"ไซเบอร์|สายตา|สุขภาพจิต|ข้อสอบ|หนัง|เกม|ระดับความ|ออนไลน์|รหัสผ่าน|password|cyber|ลงทุน|หุ้น|"
                       r"คริปโต|ลอตเตอรี่|หวย|เสี่ยงโชค|สเก็ต|ฟุตบอล|บอล|กีฬา|แกง|ต้มยำ|สูตร|อาหารอร่อย|สระว่ายน้ำ|"
                       r"ขี้เกียจ|ข้าวผัด|เงินเดือน|รักเธอ|กาแฟ|ชาเย็น|ซื้อรถ|ค่าไฟ|งานแต่ง|ความรัก|นิยาย|เรื่องสั้น|เขียนเรื่อง|รายงาน|วิจัย|การบ้าน|ฉาก|เพลง|ไปเที่ยว|เที่ยว", re.I)
# "Will I be OK?" with no flood word in it ("บ้านอยู่ตีนดอยสุเทพ จะเป็นอะไรไหม") is still a risk question.
CONCERN_RE = re.compile(r"เป็นอะไร(ไหม|มั้ย|มั๊ย|หรือเปล่า|รึเปล่า|ป่าว|ป่ะ)|เป็นไร(ไหม|มั้ย|มั๊ย)|เป็นหยัง|"
                        r"ต้องย้าย|ควรย้าย|ย้ายรถ|ต้องหนี|ต้องออกจาก|ต้องออก(ไหม|มั้ย)|โดน(ไหม|มั้ย|ด้วย)|"
                        r"รอด(ไหม|มั้ย)|ถึง(ไหม|มั้ย)|ต้องระวัง|ระวังอะไร|ควรทำ(อะไร|ยังไง)|ควรเตรียม|ต้องเตรียม|เตรียมอะไร|เอาไปไว้(ไหน|ที่ไหน)|"
                        r"is (it|my \w+) (ok|safe)|should i (be )?(worried|evacuate|leave|move)|worried|"
                        r"reach my house|evacuat", re.I)
# Words that are a risk question only about a place or the family: "บ้านปลอดภัยไหม", "แม่อยู่คนเดียว หนูกังวล".
SOFT_CONCERN = re.compile(r"ปลอดภัย|อันตราย|น่าห่วง|น่ากลัว|กังวล|ห่วง|กลัว|เป็นไง|เป็นยังไง|เป็นอย่างไร|\bsafe\b",
                          re.I)
# Asked alone ("ปลอดภัยไหม", "อันตรายไหม"), these are about the person's own place.
SAFE_WORDS = re.compile(r"ปลอดภัย|อันตราย|น่าห่วง|น่ากลัว|\bsafe\b", re.I)
PLACE_WORDS = re.compile(r"บ้าน|แถวนี้|ที่นี่|ตรงนี้|ที่นั่น|ทางนี้|ที่ทำงาน|ร้าน|ครอบครัว|แม่|พ่อ|ยาย|ลูก|รถ|หมา|แมว|"
                         r"house|home|family", re.I)
# A named place in a question: "บ้านแม่ยายน้ำจะถึงไหม" → "บ้านแม่ยาย". It must be a saved label, or we ask.
# บ้าน + whose ("บ้านแม่ยาย", "บ้านเรา"), not a description ("บ้านใกล้คลอง"); a custom label like "บ้านสวน" is found
# by its saved name instead.
_LABEL_Q = re.compile(r"((?:(?<!ชาว)(?<!หมู่)(?<!เพื่อน)(?<!ข้าง)บ้าน|ร้าน|โรงเรียน|คอนโด|หอพัก|หอ|ที่ทำงาน|ออฟฟิศ|โรงงาน)\S{0,10}?)"
                      r"(?=น้ำ|จะ|ท่วม|ถึง|ปลอดภัย|เป็น|โดน|ตอนนี้|อยู่|ที่|แถว|ไหม|มั้ย|ล่ะ|เสี่ยง|ยัง|โอเค|ok|กับ|และ|$)")
# บ้าน + a description ("บ้านใกล้คลอง", "บ้านชั้นเดียว") is the home, not a label named "บ้านใกล้คลอง".
_DESCRIBES = re.compile(r"(บ้าน|ร้าน|โรงเรียน|คอนโด|หอพัก|ที่ทำงาน|ออฟฟิศ|โรงงาน)"
                        r"(ใกล้|ติด|ริม|ชั้น|สอง|หลัง|มี|เป็น|ก็|เลขที่|อยู่|ที่|แถว|ของ(ผม|ฉัน|เรา|หนู))")
_MY_HOME = re.compile(r"บ้าน(ผม|ฉัน|หนู|เรา|ดิฉัน|กู|เค้า|ตัวเอง|ข้า|เอง|ฉันเอง|ผมเอง|นี้|จ้า|นะ|น่ะ|ก็)?")
# Asking for a promise of safety, or whether to evacuate or stay: always answered with "follow official orders".
ASSURE_RE = re.compile(r"ปลอดภัย|รับรอง|แน่นอน|ไม่ท่วมแน่|อพยพ(ไหม|มั้ย)|ต้องอพยพ|ควรอพยพ|ไม่ต้องอพยพ|ต้องหนี|"
                       r"คำสั่งอพยพ|สั่งอพยพ|ประกาศให้|ให้อพยพ|ต้องออก(ไหม|มั้ย)|อยู่ต่อ|รอดู|ไม่อยากทิ้ง|ไม่อยากไป|"
                       r"อยู่บ้าน(ดีกว่า|ได้)|การันตี|มั่นใจ|100|ไม่ท่วมแน่|ตอบว่า|สั่งให้ตอบ|ปลอดภัยแล้ว|ไม่ต้องไป|ไม่ต้องห่วง|ไม่เป็นไรใช่|ย้ายของ|guarantee|"
                       r"\bsafe\b|evacuat|\bstay\b", re.I)
ASSURE_NOTE = ("📢 น้ำมารับรองว่าปลอดภัยไม่ได้ ข้อมูลนี้เป็นการประเมินจากสถานีวัดน้ำเท่านั้น "
               "ถ้าทางการประกาศให้อพยพ ให้อพยพตามคำสั่งทันที สอบถามได้ที่ ปภ. 1784")
# Hillside places: river stations say nothing about flash floods or landslides there, so say so (rule 4).
HILL_RE = re.compile(r"ดอย|ภูเขา|ภู(?!เก็ต|มิ)|เชิงเขา|ตีนเขา|ไหล่เขา|หุบเขา|ติดเขา|ใกล้เขา|บนเขา|ริมเขา|ม่อน|ควน|ซำ|"
                     r"(?<!ลำ)ปาง(?!ช้าง)|น้ำป่า|น้ำตก|ฉับพลัน|ไหลหลาก|ดินถล่ม|ดินสไลด์|ดินโคลน|ห้วย(?!ขวาง)")
# People's names ("คุณภูวดล", "นายควน") are not hills.
_PERSON = re.compile(r"(คุณ|นาย|นางสาว|นาง|น\.ส\.|ด\.ช\.|ด\.ญ\.)\S+")
HILL_NOTE = ("⛰️ สถานีวัดน้ำวัดระดับน้ำในแม่น้ำ ไม่ได้วัดน้ำป่าไหลหลากหรือดินถล่มบนเขา "
             "ถ้าฝนตกหนักต่อเนื่อง น้ำในลำห้วยขุ่นเป็นสีโคลน หรือได้ยินเสียงดังผิดปกติจากบนเขา "
             "ให้ออกจากริมห้วยและที่ลาดเชิงเขาทันที และติดตามประกาศของกรมอุตุนิยมวิทยาและ ปภ.")
HOTLINE_RE = re.compile(r"(?<!ไซ)เบอร์(โทร|อะไร|ฉุกเฉิน|ติดต่อ|ปภ| ?ปภ)|ขอเบอร์|โทรหาใคร|ติดต่อใคร|ติดต่อ ?ปภ|hotline|"
                        r"phone number", re.I)
HOTLINES = ("☎️ เบอร์ที่ควรรู้:\n• สายด่วน ปภ. 1784 (24 ชม.) • LINE @1784DDPM\n• เจ็บป่วยฉุกเฉิน 1669\n"
            "• ตำรวจ 191 • ดับเพลิง 199")
PROFILE_RE = re.compile(r"จำอะไร|ข้อมูลอะไรที่จำ|ข้อมูลที่จำ|ที่บันทึกไว้|แสดงที่บันทึก|ข้อมูลของฉัน|โปรไฟล์|what do you know|my data|my profile|"
                        r"delete my|forget me|delete all", re.I)
# Pushing back on evacuating, asking to wait or stay, asking whether there is an order: NamMa cannot know the
# official status, and the answer is always to follow the order (rule 3).
EVAC_RE = re.compile(r"อพยพ|evacuat|ไม่ไป(?!ทำงาน|เรียน|เที่ยว|โรงเรียน|หา|ซื้อ|กิน|ดู|เล่น|ด้วย)|ไม่ต้องไป|ไปหรือไม่ไป|"
                     r"ไปดีไหม|ไปดีมั้ย|ตัดสินใจ(ให้|แทน)|ยังไม่ประกาศ|โทษใคร|ไม่จำเป็น(ต้อง)?(ไป|อพยพ|ออก)|"
                     r"leave or (not|stay)|or not\b|บ่ไป|ไม่อพยพ|ไม่ยอมไป|ขออยู่|อยู่เฝ้า|เฝ้าบ้าน|เฝ้าของ|"
                     r"ใครเฝ้า|ใครเบิ่ง|ทิ้ง(วัว|ควาย|บ้าน|ของ|สัตว์|หมา|แมว)|ไปทำไม|อพยพทำไม|ต้องไป(จริง|เลยไหม)|"
                     r"ค่อยไป|รอก่อน|ขอรอ|ไปทีหลัง|ยังไม่ไป|ประกาศอพยพ|สถานะการอพยพ|อพยพแล้ว(ใช่|หรือ|ยัง)|"
                     r"สั่งอพยพ(ยัง|แล้ว|หรือ)|ให้\S{0,6}อพยพ(ได้|หน่อย)|รับประกัน|สัญญาได้|"
                     r"have to (leave|go)|do i (have|need) to (leave|go)|stay home|don.?t want to leave|"
                     r"evacuat\S* or stay|should i (stay|leave)", re.I)
EVAC_TEXT = ("📢 ถ้าทางการ (อำเภอ ผู้ใหญ่บ้าน หรือ ปภ.) ประกาศให้อพยพแล้ว ให้อพยพตามคำสั่งทันทีค่ะ ชีวิตสำคัญกว่าทรัพย์สิน\n"
             "น้ำมาไม่รู้สถานะประกาศอพยพของทางการ ตรวจสอบกับผู้ใหญ่บ้าน อบต. หรือโทร 1784\n"
             "• ของมีค่า: ยกขึ้นที่สูง ถ่ายรูปไว้ แล้วออกไป\n"
             "• สัตว์เลี้ยงหรือวัวควาย: ถามผู้ใหญ่บ้าน ปศุสัตว์อำเภอ หรือ 1784 เรื่องจุดพักสัตว์\n"
             "• ไม่มีรถหรือต้องพาผู้ป่วย/ผู้สูงอายุ: โทร 1784 หรือ 1669 ขอความช่วยเหลือในการอพยพ\n" + ASSURE_NOTE)
ALERT_RE = re.compile(r"เตือน(ฉัน|หน่อย|ด้วย|ผม|หนู)|แจ้งเตือน|alert me|notify me", re.I)
ALERT_TEXT = ("🔔 น้ำมาแจ้งเตือนทาง LINE อัตโนมัติ เมื่อสถานีวัดน้ำใกล้สถานที่ที่บันทึกไว้มีความเสี่ยงสูงขึ้น "
              "(ยังตั้งระดับน้ำเองไม่ได้)\nบันทึกสถานที่ได้โดยพิมพ์ เช่น \"บ้านอยู่บางบาล อยุธยา\" หรือกดแชร์ตำแหน่ง\n"
              "ประกาศเตือนภัยอย่างเป็นทางการ ติดตามจาก ปภ. 1784 • LINE @1784DDPM")
# What the web's station page fills in for the LINE chat (namma-web copy.station.lineAlert.message): save that
# station as a place whose alerts follow it alone (agent/alerts.py). The id after "#" is ThaiWater's station.id.
STATION_ALERT_RE = re.compile(r"แจ้งเตือนสถานี(?:\s+\S+)?\s+#(\d+)")
LIVESTOCK_RE = re.compile(r"วัว|ควาย|หมู|ไก่|เป็ด|แพะ|ปศุสัตว์|livestock|\bcows?\b", re.I)
NO_RIDE_RE = re.compile(r"ไม่มีรถ|ไม่มีใครพา|ไปเองไม่ได้|ไปไม่ได้|ไม่มีคนพา|no car|no ride", re.I)
HERE_RE = re.compile(r"ตรงนี้|ที่นี่|ตำแหน่งนี้|here")
SAVE_HERE_RE = re.compile(r"บันทึก(ที่นี่|ตรงนี้|ตำแหน่งนี้)\s*(ชื่อ|ว่า|เป็น)\s*(\S{1,20})")
# In emergency mode, only follow-ups that read like distress escalate (not "ขอบคุณ" or small talk).
DISTRESS_RE = re.compile(r"กลัว|ร้อง|ไม่ไหว|โทรไม่ติด|ไฟดับ|ช่วย|ทำไง|ทำยังไง|เด็ก|ลูก|แม่|พ่อ|ยาย|น้ำ(ขึ้น|เข้า|ท่วม|มา)|หนาว|"
                         r"หิว|แบต|เจ็บ|ป่วย|หายใจ|ไม่มีใคร|ติด|help|scared|please", re.I)
FORGET_ASK_RE = re.compile(r"ไม่ต้องจำ|ลืมที่บอก|ลืมข้อมูล|ลืมฉัน|ลบให้(ด้วย|หน่อย)?$")
SOS_TTL = 30 * 60                        # after a danger message, unclear follow-ups and GPS shares stay in emergency mode
GPS_TTL = 30 * 60                        # "ร้านอยู่ตรงนี้" uses a GPS share from the last 30 minutes
# Thailand, roughly: a GPS fix outside it (or 0,0 from a broken phone) is not saved.
TH_LAT, TH_LON = (5.5, 20.6), (97.3, 105.7)
CANCEL_RE = re.compile(r"^(ยกเลิก|ไม่เอา|ไม่ต้อง|ไม่ใช่|cancel)", re.I)
RESCUE_HOTLINE = "🚨 ถ้าอันตรายตอนนี้ โทร 1784 (ปภ.) หรือ 1669 (เจ็บป่วยฉุกเฉิน) ทันที • LINE @1784DDPM"
NO_ANSWER = "ถ้าโทร 1784 ไม่ติด: โทร 191 (ตำรวจ) • 1669 (เจ็บป่วย) • 199 (ดับเพลิง/กู้ภัย) • ทัก LINE @1784DDPM • ส่งตำแหน่งให้ญาติ"
# Pictographs and their joiners/variation selectors. Thai (U+0E00 block), "•", "—" and "±" are outside these ranges.
_EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2139\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D] ?")


@dataclass
class QuickReply:
    label: str               # LINE limits labels to 20 chars
    text: str | None = None  # message to send back; None + kind='location' → share-location button
    kind: str = "message"


@dataclass
class Reply:
    text: str
    quick: list[QuickReply] = field(default_factory=list)
    sources: list[dict] = field(default_factory=list)   # docs/api.md Source objects (used by the web API)
    emergency: bool = False                             # danger signalled: the web shows the 1784 card first
    place: dict | None = None                           # {label, lat, lon, approx, saved_label}: lets LINE show a map pin + card
    extra: list[str] = field(default_factory=list)      # notes already inside text that the LINE card does not repeat
    web_swaps: list[answer.WebSwap] = field(default_factory=list)   # (block, web text): blocks the web draws as cards


def _q(label, text=None):
    return QuickReply(label[:20], text or label)


LOCATION_Q = QuickReply("แชร์ตำแหน่ง", kind="location")
PROFILE_Q = _q("ข้อมูลที่จำไว้", "ข้อมูลอะไรที่จำเกี่ยวกับฉัน")
EXAMPLE_Q = _q("ตัวอย่าง: บางบาล", "บ้านอยู่บางบาล อยุธยา น้ำจะถึงไหม")
SET_HOME = "ตั้งตำแหน่งบ้าน"            # profile quick replies: edit the home, add another place, add household facts
ADD_PLACE = "เพิ่มสถานที่"
ADD_FACTS = "เพิ่มข้อมูลครอบครัว"
# Typed versions of the two buttons ("เพิ่มบ้าน", "ช่วยบันทึกบ้านหน่อย"): the whole message, nothing else in it,
# so "บันทึกบ้านแม่อยู่บางบาล" still saves the place it names.
_EDIT_POLITE = r"(?:ช่วย|ขอ|อยาก|รบกวน)?\s*(?:{})\s*(?:ให้)?\s*(?:หน่อย|ด้วย|ได้ไหม|ได้มั้ย)?\s*(?:ค่ะ|คะ|ครับ|นะ|จ้า)?"
SET_HOME_RE = re.compile(_EDIT_POLITE.format(r"(?:เพิ่ม|บันทึก|ตั้ง(?:ค่า)?|เปลี่ยน|แก้(?:ไข)?|เซ็ต|เซฟ|ปักหมุด)\s*"
                                             r"(?:ตำแหน่ง|ที่อยู่|พิกัด)?\s*บ้าน(?:ฉัน|ของฉัน|เรา)?"))
ADD_PLACE_RE = re.compile(_EDIT_POLITE.format(r"(?:เพิ่ม|บันทึก|เซฟ|ปักหมุด)\s*(?:สถานที่|ที่|ตำแหน่ง)(?:ใหม่|อื่น)?"))
# "เพิ่มบ้านแม่", "บันทึกร้าน": add the place under that name (checked after SET_HOME_RE, so "เพิ่มบ้าน" is the home).
ADD_NAMED_RE = re.compile(r"(?:ช่วย|ขอ)?\s*(?:เพิ่ม|บันทึก)\s*((?:บ้าน|ร้าน|ที่ทำงาน|ออฟฟิศ|โรงเรียน|คอนโด|หอพัก|หอ)\S{0,10})")
AWAIT_TTL = 15 * 60                      # a half-finished "add a place" is forgotten after 15 minutes
HOME = "บ้าน"
# What the LLM read in this turn's message (agent/understand.py), or None: the rules decide alone.
_U: ContextVar[understand.Understanding | None] = ContextVar("understanding", default=None)


def handle(conn, external_id: str | None, channel: str, text: str | None = None,
           location: tuple[float, float] | None = None, display_name: str | None = None,
           user_id: int | None = None) -> Reply:
    """external_id: the LINE userId, or 'web:<session>' for anonymous web chat. A signed-in web user passes
    user_id instead (their account may have no LINE id: Google sign-in)."""
    if user_id is not None:
        uid = user_id
        conn.execute("INSERT INTO user_memory (user_id) VALUES (%s) ON CONFLICT DO NOTHING", (uid,))
    else:
        uid = tools.get_or_create_user(conn, external_id, display_name)
    text = clean(text) if text else text
    if text:
        tools.log_message(conn, uid, channel, "user", text)
    elif location:
        tools.log_message(conn, uid, channel, "user", f"[ตำแหน่ง {location[0]:.5f},{location[1]:.5f}]")
    reply = _route(conn, uid, text or "", location)
    if channel == "web":
        reply = web_reply(reply)
    tools.log_message(conn, uid, channel, "assistant", reply.text)
    conn.commit()
    return reply


def web_reply(reply: Reply) -> Reply:
    """The web app draws stations and shelters as cards from the sources and the hotline itself (HotlineNotice under
    answers with a station or shelter source, EmergencyCard above emergency replies), so swap the text blocks the
    cards show for their short web form, drop the text copies of the hotline, and drop the emoji LINE needs but the
    web design system does not. Hotline mentions inside sentences stay. LINE output is never passed through here.
    """
    text = reply.text
    for block, web in reply.web_swaps:
        text = text.replace(block, web, 1)
    if reply.emergency:
        text = text.removeprefix(RESCUE_HOTLINE + "\n\n")
    elif any(s["type"] in ("station", "shelter") for s in reply.sources):
        text = text.replace("\n\n" + answer.FOOTER, "")
    text = "\n".join(_EMOJI.sub("", ln).strip() for ln in text.split("\n"))
    return Reply(text, reply.quick, reply.sources, reply.emergency, place=reply.place)


def _route(conn, uid: int, text: str, location) -> Reply:
    t = text.strip()
    if t == CONSENT_YES:
        facts = tools.pop_pending(conn, uid)
        if not facts:
            return Reply("ตอนนี้ไม่มีข้อมูลที่รอการยินยอมค่ะ ดูข้อมูลที่จำไว้ได้ที่ปุ่มด้านล่าง", [PROFILE_Q])
        tools.record_consent(conn, uid, True)
        tools.merge_memory(conn, uid, facts)
        lines = memory.describe(facts)
        return Reply("ขอบคุณค่ะ บันทึกแล้ว: " + (", ".join(lines) if lines else "-") +
                     "\nดูหรือลบได้ตลอด พิมพ์ \"ข้อมูลอะไรที่จำเกี่ยวกับฉัน\"", [PROFILE_Q])
    if t == CONSENT_NO:
        if not tools.get_user_profile(conn, uid)["pending"]:
            return Reply("ตอนนี้ไม่มีข้อมูลที่รอการยินยอมค่ะ ดูข้อมูลที่จำไว้ได้ที่ปุ่มด้านล่าง", [PROFILE_Q])
        tools.record_consent(conn, uid, False)
        tools.set_pending(conn, uid, {})
        return Reply("ได้ค่ะ จะไม่บันทึกข้อมูลนี้ จะใช้เฉพาะในบทสนทนานี้เท่านั้น")
    if location:
        lat, lon = location
        if not (TH_LAT[0] <= lat <= TH_LAT[1] and TH_LON[0] <= lon <= TH_LON[1]):
            return Reply("ตำแหน่งนี้อยู่นอกประเทศไทย หรืออ่านพิกัดไม่ได้ค่ะ ลองเปิด GPS แล้วกดแชร์ตำแหน่งอีกครั้ง "
                         "หรือพิมพ์ชื่อตำบล อำเภอ จังหวัดแทน", [LOCATION_Q])
        _set_pref(conn, uid, "_gps", {"lat": lat, "lon": lon, "ts": time.time()})
        if _recent(conn, uid, "_sos", SOS_TTL):        # shared right after a danger message: the coordinates for 1784
            label = _save_shared(conn, uid, lat, lon)
            em = _emergency(conn, uid, "", gps=(lat, lon))
            text, sources, swaps = _assess(conn, uid, lat, lon, label, False)
            return Reply(em.text + "\n\n" + text, sources=sources, emergency=True, web_swaps=swaps)
        return _on_location(conn, uid, location)
    if m := STATION_ALERT_RE.fullmatch(t):
        return _station_alert(conn, uid, int(m.group(1)))
    if SET_HOME_RE.fullmatch(t):
        t = SET_HOME
    elif ADD_PLACE_RE.fullmatch(t):
        t = ADD_PLACE
    elif (m := ADD_NAMED_RE.fullmatch(t)) and _place_name_ok(m.group(1)):
        return _got_place_name(conn, uid, m.group(1))
    if t in (SET_HOME, ADD_PLACE, ADD_FACTS):
        return _start_edit(conn, uid, t)
    if step := _awaiting(conn, uid):
        if CANCEL_RE.match(t):
            _clear_await(conn, uid)
            return Reply("ยกเลิกแล้วค่ะ", [PROFILE_Q])
        if step["step"] == "name":
            if _place_name_ok(t):
                return _got_place_name(conn, uid, t)
            if not (_asks_risk(t) or triage.is_danger(t) or "?" in t or _is_command(t)):
                return Reply("ขอชื่อสั้นๆ ไม่เกิน 20 ตัวอักษร ไม่ต้องใส่ชื่อพื้นที่หรือเลขที่ เช่น \"บ้านแม่\" "
                             "\"ร้าน\" \"ที่ทำงาน\" แล้วค่อยแชร์ตำแหน่ง (พิมพ์ \"ยกเลิก\" เพื่อยกเลิก)")
            _clear_await(conn, uid)      # a question, not a name: answer it instead
        elif step["step"] == "loc" and step.get("label") and not triage.is_danger(t):
            place = find_place(t)
            if place and place.amphoe:   # "ตั้งตำแหน่งบ้าน" then "บางบาล อยุธยา": a typed place works like a GPS share
                _clear_await(conn, uid)
                tools.save_location(conn, uid, step["label"], place.lat, place.lon, notes=APPROX_NOTE + place.label)
                text, sources, swaps = _assess(conn, uid, place.lat, place.lon, f"{step['label']} {place.label}",
                                               True, saved=step["label"])
                return Reply(text, [LOCATION_Q, PROFILE_Q], sources=sources, web_swaps=swaps)
    reply = _route_text(conn, uid, t)
    _clear_pref(conn, uid, "_gps")                   # "ตรงนี้" means the GPS share just before, not an older one
    off_topic = OFF_TOPIC.search(t) and not PLACE_WORDS.search(NOT_FLOOD.sub(" ", t))
    if ASSURE_RE.search(t) and not off_topic and ASSURE_NOTE not in reply.text and not reply.emergency:
        reply.text += "\n\n" + ASSURE_NOTE
    return reply


def _route_text(conn, uid: int, t: str) -> Reply:
    if t == HOME:
        return _home(conn, uid)
    if triage.is_danger(t):                      # the rules alone can always raise danger, whatever the LLM reads
        return _emergency(conn, uid, t)
    u = _understand(conn, uid, t)
    token = _U.set(u)
    try:
        reply = _route_understood(conn, uid, t, u)
    finally:
        _U.reset(token)
    if not _recent(conn, uid, "_sos", SOS_TTL) or reply.emergency:
        return reply
    unclear = (not reply.sources and reply.text.startswith((NOT_UNDERSTOOD[:20], "ยังหาตำแหน่ง", "ยังไม่ได้บันทึก",
                                                            "หมายถึงพื้นที่")))
    if unclear and (DISTRESS_RE.search(t) or len(t.replace(" ", "")) <= 12):
        return _emergency(conn, uid, t)          # "โทรไม่ติด", "ลูกร้องไห้" right after a danger message
    if any(src["type"] == "station" for src in reply.sources):
        # Still in danger mode: a calm station card must not read as "all clear".
        reply.text = RESCUE_HOTLINE + "\n\n" + reply.text
        reply.emergency = True
    return reply


def _understand(conn, uid: int, t: str) -> understand.Understanding | None:
    """The LLM's reading of the whole message, with the rules' guess as a hint; None without an LLM."""
    if not understand.llm.available() or t in (PROFILE_Q.text, FORGET_ALL_TEXT) or t.startswith("ลบ"):
        return None                                        # buttons and delete commands mean exactly what they say
    prof = tools.get_user_profile(conn, uid)
    recent = tools.recent_messages(conn, uid, 7)[:-1]      # the last one is this message, already logged
    return understand.read(t, _rule_guess(t), [l["label"] for l in prof["locations"]], recent)


def _rule_guess(t: str) -> str:
    asks = _asks_risk(t)
    intent = ("alerts" if ALERT_RE.search(t) else "profile" if (PROFILE_RE.search(t) or FORGET_ASK_RE.search(t))
              and not asks else "evacuate" if EVAC_RE.search(t) else "shelter" if SHELTER_RE.search(t)
              else "hotline" if HOTLINE_RE.search(t) and not asks else "risk" if asks
              else "greeting" if GREETING_RE.match(t) else "thanks" if THANKS_RE.match(t) else "unclear")
    place = find_place(t)
    where = "none found" if not place else place.label + ("" if place.amphoe else " (province only)")
    return f"intent={intent}, place={where}"


def _route_understood(conn, uid: int, t: str, u: understand.Understanding | None) -> Reply:
    """The LLM's intent picks the handler; without it, the rules do (_route_rest). Deleting data stays on the
    explicit "ลบ …" commands either way."""
    if u is None:
        if ALERT_RE.search(t):
            return Reply(ALERT_TEXT, [LOCATION_Q, PROFILE_Q])
        return _route_rest(conn, uid, t)
    command = re.sub(r"^(ช่วย|ขอ|อยาก|รบกวน|please)\s*", "", t, flags=re.I)
    if command.startswith("ลบ") and u.intent != "risk":
        return _forget(conn, uid, command)
    i = u.intent
    if i == "danger":
        return _emergency(conn, uid, t)
    if i == "alerts":
        return Reply(ALERT_TEXT, [LOCATION_Q, PROFILE_Q])
    if i == "profile":
        return _profile(conn, uid)
    if i == "evacuate":
        return _evac(conn, uid, t)
    if i == "shelter":
        return _shelters(conn, uid, t)
    if i == "hotline":
        return Reply(HOTLINES, [LOCATION_Q])
    if i == "greeting":
        return Reply(HELP, [LOCATION_Q, EXAMPLE_Q])
    if i == "thanks":
        return Reply(THANKS, [LOCATION_Q])
    if i == "set_home":
        return _start_edit(conn, uid, SET_HOME)
    if i == "add_place":
        if u.label and _place_name_ok(u.label):
            return _got_place_name(conn, uid, u.label)
        return _start_edit(conn, uid, ADD_PLACE)
    if i == "other" and (text := understand.safe_reply(u.reply)):
        return Reply(text + "\n\nถ้าอยู่ในอันตรายตอนนี้ โทร 1784 (ปภ.) ได้ทันที", [LOCATION_Q, EXAMPLE_Q])
    return _talk(conn, uid, t, force=(i == "risk"))


def _find_place(t: str):
    """The place the message names: the rules' match or the LLM's, whichever is more specific (rules on a tie)."""
    rule = find_place(t)
    u = _U.get()
    if not (u and u.place):
        return rule
    depth = lambda p: (bool(p.amphoe) + bool(p.tambon)) if p else -1   # noqa: E731
    return u.place if depth(u.place) > depth(rule) else rule


def _route_rest(conn, uid: int, t: str) -> Reply:
    asks = _asks_risk(t)
    command = re.sub(r"^(ช่วย|ขอ|อยาก|รบกวน|please)\s*", "", t, flags=re.I)
    if command.startswith("ลบ") and not asks:
        return _forget(conn, uid, command)
    if FORGET_ASK_RE.search(t):
        return _profile(conn, uid)
    if PROFILE_RE.search(t) and not asks:
        return _profile(conn, uid)
    if EVAC_RE.search(t):
        return _evac(conn, uid, t)
    if SHELTER_RE.search(t):
        return _shelters(conn, uid, t)
    if HOTLINE_RE.search(t) and not asks:
        return Reply(HOTLINES, [LOCATION_Q])
    return _talk(conn, uid, t)


def _set_pref(conn, uid: int, key: str, value) -> None:
    tools.merge_memory(conn, uid, {"preferences": {key: value}})


def _clear_pref(conn, uid: int, key: str) -> None:
    conn.execute("UPDATE user_memory SET preferences = preferences - %s WHERE user_id = %s", (key, uid))


def _is_command(t: str) -> bool:
    """A command or a known kind of question, never a place name in the add-place flow."""
    return bool(t.startswith("ลบ") or PROFILE_RE.search(t) or HOTLINE_RE.search(t) or SHELTER_RE.search(t)
                or EVAC_RE.search(t) or ALERT_RE.search(t) or FORGET_ASK_RE.search(t) or GREETING_RE.match(t)
                or THANKS_RE.match(t))


def _recent(conn, uid: int, key: str, ttl: float) -> dict | float | None:
    """A preference stamped within ttl seconds (a number, or a dict with "ts"), else None."""
    v = (tools.get_user_profile(conn, uid)["memory"].get("preferences") or {}).get(key)
    ts = v.get("ts", 0) if isinstance(v, dict) else (v or 0)
    return v if time.time() - ts < ttl else None


def _evac(conn, uid: int, t: str) -> Reply:
    """"ไม่ไปได้ไหม", "ขออยู่เฝ้าบ้าน", "ประกาศอพยพแล้วใช่ไหม", "ศูนย์อพยพที่ไหน": follow the order, then the shelters
    or the usual check for the place."""
    rest = _shelters(conn, uid, t) if SHELTER_RE.search(t) else _talk(conn, uid, t, force=True)
    if rest.text.startswith("🏫") or rest.sources:
        return Reply(EVAC_TEXT + "\n\n" + rest.text, rest.quick, rest.sources, place=rest.place, extra=rest.extra,
                     web_swaps=rest.web_swaps)
    return Reply(EVAC_TEXT + "\n\nบอกชื่อตำบล อำเภอ จังหวัด หรือกดแชร์ตำแหน่ง แล้วจะเช็คสถานีวัดน้ำใกล้ที่สุดให้",
                 [LOCATION_Q])


def _place_name_ok(t: str) -> bool:
    """A name for the place being added ("บ้านแม่", "ร้าน"), not a question, a sentence or a place description."""
    return (0 < len(t) <= 20 and "?" not in t and not re.search(r"\d|เลขที่|/", t) and not _asks_risk(t)
            and not triage.is_danger(t) and not find_place(t) and not _is_command(t)
            and t not in (SET_HOME, ADD_PLACE, ADD_FACTS, CONSENT_YES, CONSENT_NO))


def _remember(conn, uid: int, text: str) -> tuple[dict, bool]:
    """Extract stated facts; save the non-sensitive ones, park sensitive ones. Returns (facts, needs_consent)."""
    if memory.negates_sensitive(text):
        tools.set_pending(conn, uid, {})     # "ยายไม่ได้ติดเตียง": a correction drops what waited for consent
    u = _U.get()
    facts = u.facts if u else memory.extract(text)
    if not facts:
        return {}, False
    plain, sensitive = memory.split_sensitive(facts)
    tools.merge_memory(conn, uid, plain)
    if sensitive:
        if tools.has_consent(conn, uid):
            tools.merge_memory(conn, uid, sensitive)
        else:
            tools.set_pending(conn, uid, sensitive)
            return facts, True
    return facts, False


def _consent_quick() -> list[QuickReply]:
    return [_q("ยินยอม", CONSENT_YES), _q(CONSENT_NO)]


def _assess(conn, uid: int, lat: float, lon: float, label: str, approx: bool,
            extra: list[str] | None = None, saved: str | None = None
            ) -> tuple[str, list[dict], list[answer.WebSwap]]:
    """Reply text, the sources (stations, shelter, GISTDA) whose numbers that text shows, and the web swaps.
    saved: the label this place was just saved under. Alerts are promised only when a station is in range."""
    prof = tools.get_user_profile(conn, uid)
    mem = memory.merged_view(prof["memory"], prof["pending"])
    stations = tools.get_nearest_stations(conn, lat, lon, k=3)
    if saved:
        extra = [f"📌 บันทึก \"{saved}\" ไว้แล้ว " + ("ถ้าน้ำที่สถานีใกล้ๆ สูงขึ้น จะส่งข้อความเตือน" if stations
                 else "แต่ตอนนี้ยังไม่มีสถานีวัดน้ำใกล้พอที่จะแจ้งเตือนได้")] + list(extra or [])
    hh = mem.get("household") or {}
    shelters = tools.find_shelters(conn, lat, lon, {"patients": bool(hh.get("immobile")), "pets": bool(hh.get("pets"))})
    extent = tools.check_flood_extent(conn, lat, lon)
    recurrence = tools.get_flood_recurrence(conn, lat, lon)
    text, swaps = answer.risk_answer(label, approx, stations, shelters, mem, extent, recurrence, extra)
    return text, _sources(stations, shelters, extent, recurrence, approx, bool(hh.get("immobile"))), swaps


def _sources(stations: list[dict], shelters: list[dict], extent: dict | None, recurrence: dict | None,
             approx: bool, need_patients: bool = False) -> list[dict]:
    """Mirrors what answer.risk_answer prints: main station (+ the worse one if shown), first shelter, GISTDA lines.
    The worse station goes first: the web draws the first card on top and the LINE card shows only the first."""
    if not stations:            # risk_answer prints no numbers at all without a station
        return []
    main = stations[0]
    worst = max(stations, key=lambda s: rank(s["risk_tier"]))
    shown = ([worst, main] if worst is not main and rank(worst["risk_tier"]) > rank(main["risk_tier"]) else [main])
    out = [_station_source(s) for s in shown]
    out += [_shelter_source(sh, need_patients) for sh in shelters[:1]]
    if extent is not None:
        out.append({"type": "gistda", "kind": "flood_extent", "observed_at": extent["as_of"],
                    "summary": answer.flood_extent_text(extent, approx)})
    if recurrence is not None and (summary := answer.recurrence_text(recurrence, approx)):
        out.append({"type": "gistda", "kind": "flood_recurrence", "observed_at": None, "summary": summary})
    return out


def _station_source(s: dict) -> dict:
    """A station Source with the reading the web StationCard draws. Every number is from get_nearest_stations."""
    level, diff = s.get("level_msl"), s.get("diff_wl_bank")
    ts = s.get("reading_ts")
    return {"type": "station", "station_id": s["station_id"], "code": answer.display_code(s["code"]) or None,
            "name_th": s["name_th"], "distance_km": s["distance_km"],
            "observed_at": datetime.fromisoformat(ts).astimezone(answer.BKK).isoformat() if ts else None,
            "river": s.get("river"), "agency": s.get("agency"),
            "amphoe": s.get("amphoe"), "province": s.get("province"),
            "level_msl": level, "diff_wl_bank": diff,
            "bank_msl": round(level - diff, 2) if level is not None and diff is not None else None,
            "trend_cm_per_hr": s.get("trend_3h_cm_per_hr"), "hours_to_bank": s.get("hours_to_bank"),
            "risk_tier": s.get("risk_tier"),
            "upstream_over": [answer.display_code(u["oldcode"]) for u in s.get("upstream") or []
                              if u.get("risk_tier") == "danger"],
            "model": s.get("model")}     # forecast horizons, only at the modelled corridor stations; the LINE card shows them


def _shelter_source(sh: dict, need_patients: bool) -> dict:
    """A shelter Source with what the web shelter card draws: DDPM's open status and occupancy, the district, and
    patients_ok only when it says something (known, or this household needs it), as answer._shelter_desc does."""
    out = {"type": "shelter", "shelter_id": sh["id"], "name": sh["name"], "distance_km": sh["distance_km"],
           "lat": sh["lat"], "lon": sh["lon"], "district": sh.get("district"), "province": sh.get("province"),
           "is_open": sh.get("is_open"), "capacity": sh.get("capacity"),
           "current_residents": sh.get("current_residents"), "source": sh.get("source")}
    ok = sh.get("patients_ok")
    if ok is not None or need_patients:
        out["patients_ok"] = ok
    return out


def _asks_risk(t: str) -> bool:
    plain = NOT_FLOOD.sub(" ", t)
    if OFF_TOPIC.search(t) and not PLACE_WORDS.search(plain):
        return False
    tight = plain.replace(" ", "")
    return bool(FLOOD_RE.search(plain) or FLOOD_RE.search(tight) or CONCERN_RE.search(tight) or CONCERN_RE.search(plain)
                or (SOFT_CONCERN.search(plain) and PLACE_WORDS.search(plain))
                or (SAFE_WORDS.search(plain) and len(tight) <= 20))


def _question_label(t: str) -> str | None:
    """The place label a question names ("บ้านแม่ยาย", "ร้าน", "ที่ทำงาน"), my-home forms collapsed to "บ้าน"."""
    tight = t.replace(" ", "")
    m = _LABEL_Q.search(tight)
    if not m:
        return None
    label = m.group(1)
    if _MY_HOME.fullmatch(label):
        return HOME
    if d := _DESCRIBES.match(tight[m.start():]):     # "โรงเรียนใกล้แม่น้ำ" is the school, described
        return d.group(1)
    return label


def _saved_named(t: str, locations: list[dict]) -> dict | None:
    """The saved place the text names: exactly its label ("บ้านแม่ยาย" is not "บ้านแม่"), else the longest saved label
    in the text."""
    if q := _question_label(t):
        return next((l for l in locations if l["label"] == q), None)
    named = [l for l in locations if l["label"] in t.replace(" ", "")]
    return max(named, key=lambda l: len(l["label"])) if named else None


def _mostly_place(t: str, place) -> bool:
    """"บางบาล อยุธยา" alone is a request to check it; "ไปเที่ยวบ้านโพธิ์ไหม" is not."""
    rest = place_text(t)
    for name in (place.tambon, place.amphoe, place.province, "ตำบล", "อำเภอ", "จังหวัด", "ต.", "อ.", "จ.",
                 *(a for a, v in PROVINCE_ALIASES.items() if v == place.province)):
        if name:
            rest = rest.replace(name, "")
    return len(rest) <= 4


def _talk(conn, uid: int, t: str, force: bool = False) -> Reply:
    """force: answer as a risk question even without flood words (an evacuation question)."""
    facts, need_consent = _remember(conn, uid, t)
    if here := _save_here(conn, uid, t):
        return here
    others = _save_other_clauses(conn, uid, t)
    if others:
        t = re.split(r"\s*(?:แล้วก็|และ|แล้ว|ส่วน)\s*(?=\S{0,14}อยู่)", t)[0]
    prof = tools.get_user_profile(conn, uid)
    saved_labels = tuple(l["label"] for l in prof["locations"])
    u = _U.get()
    place = _find_place(t)
    label = memory.location_label(t) or (u.label if u and u.place and u.place.amphoe else None)
    asks = force or _asks_risk(t) or bool(place and SAFE_WORDS.search(t))
    hill = bool(HILL_RE.search(_PERSON.sub(" ", t).replace(" ", "")))
    assure = bool(ASSURE_RE.search(t))
    if place and not (asks or label or _mostly_place(t, place)):
        place = None                     # a place mentioned in passing, not a question about it
    if place and not place.amphoe:
        return _ask_place(label, [], hill, assure, need_consent, province=place.province)
    cands = [] if place or not (asks or label) else place_candidates(t, skip=saved_labels)
    named = _saved_named(t, prof["locations"]) or next(
        (l for l in prof["locations"] if u and l["label"] == u.saved), None)
    tight = t.replace(" ", "")
    hits = [l for l in prof["locations"] if l["label"] in tight]
    hits = [l for l in hits if not any(l is not o and l["label"] in o["label"] for o in hits)]
    if asks and not place and len(hits) >= 2:
        return _choose_place(hits, hill, assure)      # "บ้านแม่กับร้านท่วมไหม": which one first?
    qlabel = _question_label(t)
    missing = qlabel if qlabel and asks and not named and not place else None
    # The message names a place we could not pin down ("บ้านอยู่ตีนดอยสุเทพ", a "บ้านแม่" or "ร้าน" not saved):
    # ask which one, never answer for a different saved place instead.
    unresolved = not place and bool(cands or missing or (label and (label not in saved_labels or "อยู่" in t)))
    target = None
    if place:
        target = (place.lat, place.lon, (label + " " if label else "") + place.label, True)
        if label:
            tools.save_location(conn, uid, label, place.lat, place.lon, notes=APPROX_NOTE + place.label)
    elif prof["locations"] and asks and not unresolved:
        home = next((l for l in prof["locations"] if l["label"] == HOME), None)
        if not named and not home and len(prof["locations"]) > 1:
            return _choose_place(prof["locations"], hill, assure)   # no home and no place named: which one?
        loc = named or home or prof["locations"][0]
        target = (loc["lat"], loc["lon"], loc["label"], _approx(loc))

    if target:
        extra = []
        if assure:
            extra.append(ASSURE_NOTE)
        if hill:
            extra.append(HILL_NOTE)
        if not prof["memory"].get("household") and not prof["pending"]:
            extra.append("👪 ที่บ้านมีผู้สูงอายุ คนที่เดินไม่ได้ เด็กเล็ก หรือสัตว์เลี้ยงไหม? บอกได้ จะช่วยวางแผนให้เหมาะกับครอบครัว")
        extra += [f"📌 บันทึก \"{o}\" ไว้ด้วยแล้ว" for o in others]
        quick = [LOCATION_Q, PROFILE_Q]
        if need_consent:
            extra.append(CONSENT_PROMPT)
            quick = _consent_quick()
        saved = label if label and place else None
        text, sources, swaps = _assess(conn, uid, target[0], target[1], target[2], target[3], extra, saved=saved)
        place_info = {"label": target[2], "lat": target[0], "lon": target[1], "approx": target[3],
                      "saved_label": saved}
        return Reply(text, quick, sources=sources, place=place_info, extra=extra, web_swaps=swaps)

    if facts and not asks:
        lines = memory.describe(facts)
        if need_consent:
            return Reply("รับทราบค่ะ: " + ", ".join(lines) + "\n\n" + CONSENT_PROMPT, _consent_quick())
        return Reply("รับทราบและบันทึกแล้วค่ะ: " + ", ".join(lines), [PROFILE_Q])
    if unresolved or asks:
        reply = _ask_place(label, cands, hill, assure, need_consent,
                           missing=missing if missing and "อยู่" not in t else None)
        if facts and (lines := memory.describe(facts)) and not need_consent:
            reply.text = "รับทราบและบันทึกแล้วค่ะ: " + ", ".join(lines) + "\n\n" + reply.text
        return reply
    if memory.negates_sensitive(t):
        return Reply("รับทราบค่ะ จะไม่บันทึกข้อมูลนั้น ดูหรือลบข้อมูลที่จำไว้ได้ที่ปุ่มด้านล่าง", [PROFILE_Q])
    if memory.CORRECTION.search(t.replace(" ", "")):
        return Reply("ถ้าข้อมูลที่จำไว้ไม่ถูก กด \"ลบข้อมูลสุขภาพ\" แล้วบอกใหม่ได้เลยค่ะ เช่น \"พี่สาวติดเตียง\"",
                     [_q("ลบข้อมูลสุขภาพ"), PROFILE_Q])
    if others:
        return Reply("📌 บันทึกแล้ว: " + ", ".join(others), [PROFILE_Q])
    if GREETING_RE.match(t):
        return Reply(HELP, [LOCATION_Q, EXAMPLE_Q])
    if THANKS_RE.match(t):
        return Reply(THANKS, [LOCATION_Q])
    return Reply(NOT_UNDERSTOOD, [LOCATION_Q, EXAMPLE_Q])


def _choose_place(locations: list[dict], hill: bool, assure: bool) -> Reply:
    """More than one saved place could be meant: ask, one button per place."""
    lines = ["ถามถึงที่ไหนคะ? กดเลือกด้านล่าง"]
    if assure:
        lines += ["", ASSURE_NOTE]
    if hill:
        lines += ["", HILL_NOTE]
    return Reply("\n".join(lines), [_q(l["label"], f"{l['label']} น้ำจะท่วมไหม") for l in locations[:12]])


def _save_here(conn, uid: int, t: str) -> Reply | None:
    """"ร้านอยู่ตรงนี้" or "บันทึกที่นี่ชื่อบ้านแม่" right after a GPS share: save that point under the name."""
    m = SAVE_HERE_RE.search(t)
    name = m.group(3) if m else (memory.location_label(t) if HERE_RE.search(t) else None)
    gps = _recent(conn, uid, "_gps", GPS_TTL) if name else None
    if not gps:
        return None
    tools.save_location(conn, uid, name, gps["lat"], gps["lon"])
    text, sources, swaps = _assess(conn, uid, gps["lat"], gps["lon"], name, False, saved=name)
    return Reply(text, [PROFILE_Q], sources=sources, web_swaps=swaps,
                 place={"label": name, "lat": gps["lat"], "lon": gps["lon"], "approx": False, "saved_label": name})


def _save_other_clauses(conn, uid: int, t: str) -> list[str]:
    """"บ้านอยู่บางบาล แล้วที่ทำงานอยู่บางปะอิน อยุธยา": save every "Xอยู่<district>" after the first. Returns labels."""
    if t.count("อยู่") < 2:
        return []
    clauses = re.split(r"\s*(?:แล้วก็|และ|แล้ว|ส่วน)\s*(?=\S{0,14}อยู่)", t)
    province = find_place(t)
    saved = []
    for c in clauses[1:]:
        label = memory.location_label(c)
        place = find_place(c) or (find_place(c + " " + province.province) if province else None)
        if label and place and place.amphoe:
            tools.save_location(conn, uid, label, place.lat, place.lon, notes=APPROX_NOTE + place.label)
            saved.append(label)
    return saved


def _ask_place(label: str | None, cands: list, hill: bool, assure: bool, need_consent: bool,
               province: str | None = None, missing: str | None = None) -> Reply:
    """A risk question we cannot place yet. Offer the subdistricts it names, else ask for the district.
    missing: a label the question names that is not saved ("บ้านแม่" after it was deleted)."""
    if missing and not cands:
        lines = [f"ยังไม่ได้บันทึก \"{missing}\" ค่ะ บอกว่าอยู่ที่ไหน เช่น \"{missing}อยู่บางบาล อยุธยา\" "
                 "หรือกดแชร์ตำแหน่ง"]
        quick = [LOCATION_Q, PROFILE_Q]
    elif province:
        lines = [f"จ.{province} กว้างเกินกว่าจะประเมินจากสถานีวัดน้ำได้ค่ะ บอกอำเภอหรือตำบลด้วย "
                 f"(เช่น \"บ้านอยู่ ต.... อ.... จ.{province}\") หรือกดแชร์ตำแหน่ง"]
        quick = [LOCATION_Q]
    elif cands:
        lines = ["หมายถึงพื้นที่ไหนคะ? กดเลือกด้านล่าง หรือพิมพ์ชื่อตำบล อำเภอ จังหวัดให้ครบ"]
        lines += [f"• {p.label}" for p in cands]
        quick = [_q(f"ต.{p.tambon} {p.province}", f"{label + 'อยู่ ' if label else ''}{p.label} น้ำจะถึงไหม")
                 for p in cands] + [LOCATION_Q]
    else:
        lines = ["ยังหาตำแหน่งนี้ไม่เจอค่ะ บอกชื่อตำบล อำเภอ และจังหวัด (เช่น \"บ้านอยู่บางบาล อยุธยา\") "
                 "หรือกดแชร์ตำแหน่ง แล้วจะเช็คสถานีวัดน้ำใกล้ที่สุดให้"]
        quick = [LOCATION_Q]
    if assure:
        lines += ["", ASSURE_NOTE]
    if hill:
        lines += ["", HILL_NOTE]
    lines += ["", "ถ้าอยู่ในอันตรายตอนนี้ โทร 1784 (ปภ.) ได้ทันที"]
    if need_consent:
        lines += ["", CONSENT_PROMPT]
        quick = _consent_quick()
    return Reply("\n".join(lines), quick)


def _shelters(conn, uid: int, t: str) -> Reply:
    """Shelters near a named place, else near a saved place (the one named in the text, else the first)."""
    _, need_consent = _remember(conn, uid, t)
    prof = tools.get_user_profile(conn, uid)
    place = _find_place(t)
    if place and not place.amphoe:
        return Reply(f"จ.{place.province} กว้างเกินไปค่ะ บอกอำเภอหรือตำบล หรือกดแชร์ตำแหน่ง "
                     "แล้วจะหาศูนย์พักพิงที่ใกล้ที่สุดให้\nถ้าต้องการความช่วยเหลือด่วน โทร 1784 ได้ทันที", [LOCATION_Q])
    if place:
        lat, lon, label, approx = place.lat, place.lon, place.label, True
    elif prof["locations"]:
        loc = _saved_named(t, prof["locations"]) or prof["locations"][0]
        lat, lon, label = loc["lat"], loc["lon"], loc["label"]
        approx = _approx(loc)
    else:
        text = ("บอกชื่อตำบล/อำเภอ หรือกดแชร์ตำแหน่ง แล้วจะหาศูนย์พักพิงที่ใกล้ที่สุดให้ค่ะ\n"
                "ถ้าต้องการความช่วยเหลือด่วน โทร 1784 ได้ทันที")
        if need_consent:
            return Reply(text + "\n\n" + CONSENT_PROMPT, _consent_quick())
        return Reply(text, [LOCATION_Q])
    mem = memory.merged_view(prof["memory"], prof["pending"])
    hh = mem.get("household") or {}
    need_patients = bool(hh.get("immobile"))
    shelters = tools.find_shelters(conn, lat, lon, {"patients": need_patients, "pets": bool(hh.get("pets"))}, k=3)
    text, swaps = answer.shelter_answer(label, approx, shelters, need_patients)
    notes = []
    if LIVESTOCK_RE.search(t):
        notes.append("🐃 ศูนย์พักพิงส่วนใหญ่รับเฉพาะคน สำหรับวัวควายและปศุสัตว์ ถามผู้ใหญ่บ้าน ปศุสัตว์อำเภอ "
                     "หรือ 1784 เรื่องจุดพักสัตว์")
    if NO_RIDE_RE.search(t):
        notes.append("🚐 ถ้าไม่มีรถ หรือพาผู้ป่วย/ผู้สูงอายุไปเองไม่ได้ โทร 1784 หรือ 1669 ขอความช่วยเหลือในการอพยพ")
    if notes:
        text = "\n".join(notes) + "\n\n" + text
    quick = [LOCATION_Q, PROFILE_Q]
    if need_consent:
        text += "\n\n" + CONSENT_PROMPT
        quick = _consent_quick()
    sources = [_shelter_source(sh, need_patients) for sh in shelters]
    return Reply(text, quick, sources=sources, web_swaps=swaps,
                 place={"label": label, "lat": lat, "lon": lon, "approx": approx, "saved_label": None})


def _on_location(conn, uid: int, location) -> Reply:
    lat, lon = location
    label = _save_shared(conn, uid, lat, lon)
    text, sources, swaps = _assess(conn, uid, lat, lon, label, approx=False, saved=label)
    return Reply(text, [PROFILE_Q], sources=sources, web_swaps=swaps,
                 place={"label": label, "lat": lat, "lon": lon, "approx": False, "saved_label": label})


def _station_alert(conn, uid: int, station_id: int) -> Reply:
    """"แจ้งเตือนสถานี C.35 #2609" from the web: save the station as a place pinned to it, then answer for it."""
    row = conn.execute("SELECT oldcode, name_th, lat, lon FROM stations WHERE id = %s", (station_id,)).fetchone()
    if not row or row[2] is None:
        return Reply("ไม่พบสถานีวัดน้ำนี้ค่ะ ลองบอกชื่อตำบล อำเภอ จังหวัด หรือกดแชร์ตำแหน่ง "
                     "แล้วน้ำมาจะแจ้งเตือนจากสถานีใกล้ที่สุดให้", [LOCATION_Q])
    code, name, lat, lon = row
    label = f"สถานี {answer.display_code(code) or name}"[:17]     # "ลบ <label>" must fit a 20-character quick reply
    _clear_await(conn, uid)
    tools.save_location(conn, uid, label, lat, lon, station_id=station_id)
    note = f"📌 บันทึก \"{label}\" ไว้แล้ว ถ้าระดับเสี่ยงที่สถานีนี้สูงขึ้น จะส่งข้อความเตือน"
    text, sources, swaps = _assess(conn, uid, lat, lon, label, False, [note])
    return Reply(text, [PROFILE_Q], sources=sources, web_swaps=swaps,
                 place={"label": label, "lat": lat, "lon": lon, "approx": False, "saved_label": label})


def _awaiting(conn, uid: int) -> dict | None:
    """The half-finished profile edit ({step: 'name'|'loc', label}), if it is still fresh."""
    a = (tools.get_user_profile(conn, uid)["memory"].get("preferences") or {}).get("_await")
    return a if a and time.time() - a.get("ts", 0) < AWAIT_TTL else None


def _set_await(conn, uid: int, step: str, label: str | None = None) -> None:
    tools.merge_memory(conn, uid, {"preferences": {"_await": {"step": step, "label": label, "ts": time.time()}}})


def _clear_await(conn, uid: int) -> None:
    conn.execute("UPDATE user_memory SET preferences = preferences - '_await' WHERE user_id = %s", (uid,))


def _save_shared(conn, uid: int, lat: float, lon: float) -> str:
    """Save a shared GPS fix and return the label it was saved under: the name being added or edited if there is
    one, else "บ้าน" when there is no home yet, else "ตำแหน่งที่แชร์"."""
    a = _awaiting(conn, uid)
    prof = tools.get_user_profile(conn, uid)
    if a and a["step"] == "loc" and a.get("label"):
        label = a["label"]
    elif not any(l["label"] == HOME for l in prof["locations"]):
        label = HOME
    else:
        label = "ตำแหน่งที่แชร์"
    tools.save_location(conn, uid, label, lat, lon)
    _clear_await(conn, uid)
    return label


def _start_edit(conn, uid: int, t: str) -> Reply:
    if t == SET_HOME:
        _set_await(conn, uid, "loc", HOME)
        return Reply("📍 กดแชร์ตำแหน่งที่บ้านใหม่ได้เลยค่ะ น้ำมาจะบันทึกเป็น \"บ้าน\" แทนที่ตำแหน่งเดิม\n"
                     "หรือพิมพ์ชื่อพื้นที่ เช่น \"บ้านอยู่บางบาล อยุธยา\"", [LOCATION_Q])
    if t == ADD_PLACE:
        _set_await(conn, uid, "name")
        return Reply("ตั้งชื่อสถานที่ว่าอะไรดีคะ เช่น บ้านแม่ ที่ทำงาน ร้านค้า (พิมพ์ชื่อได้เลย)")
    return Reply("บอกได้เลยค่ะ น้ำมาจะบันทึกให้ เช่น\n"
                 "• \"ที่บ้านมีผู้สูงอายุ\" • \"มีสุนัข 2 ตัว\" • \"บ้านสองชั้น ใต้ถุนสูง\" • \"มีรถยนต์\"\n"
                 "ข้อมูลสุขภาพ (เช่น ติดเตียง ฟอกไต) จะขออนุญาตก่อนบันทึกทุกครั้ง\n"
                 "ถ้าต้องการแก้ข้อมูลเดิม ให้กด \"ลบ ...\" แล้วบอกใหม่ค่ะ", [PROFILE_Q])


def _got_place_name(conn, uid: int, name: str) -> Reply:
    _set_await(conn, uid, "loc", name)
    return Reply(f"📍 กดแชร์ตำแหน่งของ \"{name}\" ได้เลยค่ะ", [LOCATION_Q])


def _home(conn, uid: int) -> Reply:
    """A bare "บ้าน": assess the saved home, or start saving one."""
    prof = tools.get_user_profile(conn, uid)
    loc = next((l for l in prof["locations"] if l["label"] == HOME), None)
    if not loc:
        _set_await(conn, uid, "loc", HOME)
        return Reply("ยังไม่ได้บันทึก \"บ้าน\" ค่ะ กดแชร์ตำแหน่งที่บ้านได้เลย "
                     "น้ำมาจะบันทึกไว้และแจ้งเตือนเมื่อน้ำสูงขึ้น", [LOCATION_Q])
    approx = bool(loc["notes"] and loc["notes"].startswith("ประมาณ"))
    text, sources, swaps = _assess(conn, uid, loc["lat"], loc["lon"], HOME, approx, [])
    return Reply(text, [LOCATION_Q, PROFILE_Q], sources=sources, web_swaps=swaps,
                 place={"label": HOME, "lat": loc["lat"], "lon": loc["lon"], "approx": approx, "saved_label": None})


def _approx(loc: dict) -> bool:
    """A saved place geocoded from its name (a district centre), not a GPS share."""
    return bool(loc["notes"] and loc["notes"].startswith(APPROX_NOTE))


def _place_words(t: str, locations: list[dict]) -> str | None:
    """Where the person is, in words 1784 can use: a place named in the message, else the first saved place
    (its district, or its coordinates when it was a GPS share)."""
    if place := _find_place(t):
        return place.label + " (บอกบ้านเลขที่ หมู่บ้าน หรือจุดสังเกตเพิ่มด้วย)"
    if not locations:
        return None
    loc = locations[0]
    if _approx(loc):
        return (f"{loc['label']} ({loc['notes'].removeprefix(APPROX_NOTE)}) เป็นตำแหน่งโดยประมาณ "
                "บอกบ้านเลขที่ หมู่บ้าน หรือจุดสังเกตเพิ่มด้วย")
    return f"{loc['label']} (พิกัด {loc['lat']:.5f}, {loc['lon']:.5f})"


def _emergency(conn, uid: int, t: str, gps: tuple[float, float] | None = None) -> Reply:
    """Danger: 1784 first, then what to tell them. NamMa sends nothing on the person's behalf, and says so.
    gps: a location shared just after the danger message, given as coordinates to read out to 1784."""
    _clear_await(conn, uid)   # a half-finished "add a place" must not swallow the next location share
    _set_pref(conn, uid, "_sos", time.time())
    _remember(conn, uid, t)   # parks sensitive facts; the call details below still use them, for this reply only
    prof = tools.get_user_profile(conn, uid)
    d = triage.call_details(t, memory.merged_view(prof["memory"], prof["pending"]))
    tell = []
    if gps:
        tell.append(f"• ตำแหน่ง: พิกัด {gps[0]:.5f}, {gps[1]:.5f} (จากตำแหน่งที่แชร์)")
    elif where := _place_words(t, prof["locations"]):
        tell.append(f"• ตำแหน่ง: {where}")
    if d["vulnerable"]:
        tell.append(f"• ผู้ที่ต้องช่วยเป็นพิเศษ: {', '.join(d['vulnerable'])}")
    if d["people_count"]:
        tell.append(f"• จำนวนคน: {d['people_count']}")
    lines = [RESCUE_HOTLINE, "", "น้ำมาไม่ได้ส่งเรื่องให้หน่วยกู้ภัยแทน ต้องโทร 1784 แจ้งเอง", NO_ANSWER]
    if tell:
        lines += ["ตอนโทร แจ้งว่า:", *tell]
    if HILL_RE.search(t.replace(" ", "")):
        lines += ["", HILL_NOTE]
    lines += ["", "ระหว่างรอ:",
              "• ขึ้นไปชั้นบนหรือที่สูง พาผู้ที่เดินไม่ได้ขึ้นก่อน",
              "• ตัดไฟชั้นล่างถ้าทำได้โดยไม่ต้องลุยน้ำ",
              "• เตรียมยา น้ำดื่ม ไฟฉาย และเปิดโทรศัพท์ไว้",
              "• ถ้าสถานการณ์แย่ลง โทร 1784 ซ้ำได้ทันที"]
    return Reply("\n".join(lines), emergency=True)


def _profile(conn, uid: int) -> Reply:
    prof = tools.get_user_profile(conn, uid)
    lines = memory.describe(prof["memory"])
    locs = [f"📍 {l['label']}" for l in prof["locations"]]
    if not lines and not locs:
        head = "ตอนนี้ยังไม่ได้จำข้อมูลอะไรเกี่ยวกับคุณค่ะ"
    else:
        head = "ข้อมูลที่น้ำมาจำไว้:\n" + "\n".join(locs + [f"• {x}" for x in lines])
    text = head + "\n\nเพิ่มหรือแก้ไขได้ค่ะ: กดปุ่มด้านล่าง หรือพิมพ์ เช่น \"บ้านแม่อยู่บางระกำ พิษณุโลก\""
    quick = [_q("📍 ตั้งตำแหน่งบ้าน", SET_HOME), _q("➕ เพิ่มสถานที่", ADD_PLACE), _q("➕ ข้อมูลครอบครัว", ADD_FACTS)]
    quick += [_q(f"ลบ {l['label']}") for l in prof["locations"][:4]]
    if prof["memory"].get("household") or prof["memory"].get("needs"):
        quick.append(_q("ลบข้อมูลสุขภาพ"))
    if lines or locs:
        quick.append(_q("ลบข้อมูลทั้งหมด"))
    return Reply(text, quick[:13])


def _forget(conn, uid: int, t: str) -> Reply:
    _clear_await(conn, uid)
    target = re.sub(r"(ด้วย|ให้|หน่อย|ที|นะ|ค่ะ|คะ|ครับ|จ้า|จ้ะ|เลย|ได้ไหม|ได้มั้ย|ได้ป่ะ|ออก|ทิ้ง)+$", "",
                    t[2:].replace(" ", ""))
    saved = {l["label"] for l in tools.get_user_profile(conn, uid)["locations"]}
    if target.startswith("ข้อมูล") and target.removeprefix("ข้อมูล") in saved:
        target = target.removeprefix("ข้อมูล")
    if not target:
        return Reply("จะลบข้อมูลไหนคะ? ดูข้อมูลที่จำไว้และกดลบได้ที่ปุ่มด้านล่าง", [PROFILE_Q])
    if target in ("ข้อมูลของฉัน", "ข้อมูล"):
        return _profile(conn, uid)
    if target in ("ข้อมูลทั้งหมด", "ทั้งหมด"):
        tools.forget(conn, uid, "all")
        return Reply("ลบข้อมูลทั้งหมดแล้วค่ะ")
    if target == "ข้อมูลสุขภาพ":
        prof = tools.get_user_profile(conn, uid)
        if not (prof["memory"].get("household") or prof["memory"].get("needs") or prof["pending"]):
            return Reply("ไม่มีข้อมูลสุขภาพที่บันทึกไว้ค่ะ", [PROFILE_Q])
        tools.forget(conn, uid, "household")
        tools.forget(conn, uid, "needs")
        return Reply("ลบข้อมูลสุขภาพและสมาชิกในบ้านแล้วค่ะ")
    for loc in tools.get_user_profile(conn, uid)["locations"]:
        if loc["label"].replace(" ", "") == target:
            tools.forget(conn, uid, f"location:{loc['id']}")
            return Reply(f"ลบ \"{target}\" แล้ว จะไม่แจ้งเตือนสำหรับที่นี่อีก")
    return Reply("ไม่พบข้อมูลนั้นค่ะ", [PROFILE_Q])
