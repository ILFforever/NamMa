"""line.webhook._handle_event: an answer is never lost, even when the reply token has expired."""
import contextlib

import pytest

from agent import chat
from line import webhook


@pytest.fixture()
def sent(monkeypatch):
    calls = []
    monkeypatch.setattr(webhook, "connect", lambda: contextlib.nullcontext(None))
    monkeypatch.setattr(webhook.chat, "handle", lambda conn, src, ch, **kw: chat.Reply("คำตอบ"))
    monkeypatch.setattr(webhook.cards, "risk_messages", lambda r: None)
    monkeypatch.setattr(webhook.client, "push", lambda to, msgs: calls.append(("push", to, msgs[0]["text"])))
    return calls


EVENT = {"type": "message", "replyToken": "tok", "source": {"userId": "U1"}, "message": {"type": "text", "text": "Hello"}}


def test_reply_used_when_it_works(sent, monkeypatch):
    monkeypatch.setattr(webhook.client, "reply", lambda tok, msgs: sent.append(("reply", tok, msgs[0]["text"])))
    webhook._handle_event(EVENT)
    assert sent == [("reply", "tok", "คำตอบ")]


def test_push_when_reply_token_rejected(sent, monkeypatch):
    def expired(tok, msgs):
        raise RuntimeError("400 Invalid reply token")
    monkeypatch.setattr(webhook.client, "reply", expired)
    webhook._handle_event(EVENT)
    assert sent == [("push", "U1", "คำตอบ")]
