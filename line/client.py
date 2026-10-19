"""LINE Messaging API: signature check, reply, push. Docs: developers.line.biz/en/docs/messaging-api/"""
import base64
import hashlib
import hmac
import os

import httpx

API = "https://api.line.me/v2/bot/message"
MAX_TEXT = 5000


def configured() -> bool:
    return bool(os.environ.get("LINE_CHANNEL_SECRET") and os.environ.get("LINE_CHANNEL_ACCESS_TOKEN"))


def verify_signature(body: bytes, signature: str | None) -> bool:
    secret = os.environ.get("LINE_CHANNEL_SECRET", "")
    if not secret or not signature:
        return False
    mac = hmac.new(secret.encode(), body, hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(mac).decode(), signature)


def quick_reply(quick: list | None) -> dict | None:
    """quick: agent.chat.QuickReply list. LINE allows 13 items."""
    items = []
    for q in (quick or [])[:13]:
        if q.kind == "location":
            items.append({"type": "action", "action": {"type": "location", "label": q.label[:20]}})
        else:
            items.append({"type": "action", "action": {"type": "message", "label": q.label[:20], "text": q.text[:300]}})
    return {"items": items} if items else None


def text_message(text: str, quick: list | None = None) -> dict:
    msg = {"type": "text", "text": text[:MAX_TEXT]}
    qr = quick_reply(quick)
    if qr:
        msg["quickReply"] = qr
    return msg


def _post(path: str, payload: dict) -> None:
    r = httpx.post(f"{API}/{path}", json=payload, timeout=10,
                   headers={"Authorization": f"Bearer {os.environ['LINE_CHANNEL_ACCESS_TOKEN']}"})
    if r.status_code >= 400:
        print(f"LINE {path} {r.status_code}: {r.text[:300]}", flush=True)
    r.raise_for_status()


def reply(reply_token: str, messages: list[dict]) -> None:
    _post("reply", {"replyToken": reply_token, "messages": messages[:5]})


def push(line_user_id: str, messages: list[dict]) -> None:
    _post("push", {"to": line_user_id, "messages": messages[:5]})
