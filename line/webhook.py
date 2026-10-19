"""LINE webhook → agent.chat.handle → reply. Mounted by api.main at POST /line/webhook."""
import json
import time

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from agent import chat
from common.db import connect
from line import cards, client

router = APIRouter()

WELCOME = ("สวัสดีค่ะ น้ำมา (NamMa) ผู้ช่วยเรื่องน้ำท่วม 🌊\n"
           "บอกชื่อพื้นที่ เช่น \"บ้านแม่อยู่บางบาล อยุธยา น้ำจะถึงไหม\" หรือแชร์ตำแหน่ง\n"
           "แล้วน้ำมาจะดูสถานีวัดน้ำใกล้ที่สุดและแจ้งเตือนเมื่อน้ำสูงขึ้น\n\n")


FALLBACK = ("ขออภัย ระบบขัดข้องชั่วคราว ลองใหม่อีกครั้งในอีกสักครู่\n"
            "🚨 ถ้าอันตรายตอนนี้ โทร 1784 (ปภ.) หรือ 1669 ทันที • LINE @1784DDPM")


def _handle_event(ev: dict) -> None:
    src = (ev.get("source") or {}).get("userId")
    token = ev.get("replyToken")
    if not src or not token:
        return
    msg = ev.get("message") or {}
    t0 = time.monotonic()
    try:
        with connect() as conn:
            if ev["type"] == "follow":
                r = chat.Reply(WELCOME + chat.answer.FOOTER, [chat.LOCATION_Q])
            elif ev["type"] == "message" and msg.get("type") == "text":
                r = chat.handle(conn, src, "line", text=msg["text"])
            elif ev["type"] == "message" and msg.get("type") == "location":
                r = chat.handle(conn, src, "line", location=(msg["latitude"], msg["longitude"]))
            else:
                return
    except Exception as e:
        # Never leave someone who may be in danger with no answer.
        print(f"chat failed for LINE event: {e!r}", flush=True)
        r = chat.Reply(FALLBACK)
    took = time.monotonic() - t0
    print(f"LINE {ev['type']}/{msg.get('type', '-')} answered in {took:.1f}s", flush=True)
    text_only = [client.text_message(r.text, r.quick)]
    try:
        rich = cards.risk_messages(r)
    except Exception as e:
        print(f"LINE card build failed: {e!r}", flush=True)
        rich = None
    if rich:
        qr = client.quick_reply(r.quick)
        if qr:
            rich[-1]["quickReply"] = qr
        try:
            client.reply(token, rich)
            return
        except Exception as e:   # a rejected card must never cost the user their answer
            print(f"LINE rich reply failed, sending text: {e!r}", flush=True)
    try:
        client.reply(token, text_only)
    except Exception as e:
        # The reply token expires (~1 min) or was rejected: push the answer instead (costs push quota,
        # but an unanswered message is worse, above all from someone who may be in danger).
        print(f"LINE reply failed after {took:.1f}s, pushing instead: {e!r}", flush=True)
        client.push(src, text_only)


@router.post("/line/webhook")
async def webhook(request: Request, background: BackgroundTasks):
    body = await request.body()
    if not client.verify_signature(body, request.headers.get("x-line-signature")):
        raise HTTPException(401, "bad signature")
    for ev in json.loads(body).get("events", []):
        background.add_task(_handle_event, ev)   # ack LINE fast; the reply token lives ~1 min
    return {"ok": True}
